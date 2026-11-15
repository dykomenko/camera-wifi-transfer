"""Move every photo and video off a Sony camera card, keeping the card's
image database consistent so the camera reports no error afterwards.

Order of operations (nothing on the card changes until step 4):

1. ``open_card`` checks the Sony folder layout, validates the image
   database and requires the media files on the card to match it exactly.
2. Every file is copied to the destination through a ``.part`` file.
3. Each copy is read back and compared with the original by SHA-256.
4. The card's database files are backed up to the PC, then the media
   files are deleted from the card and the database is rewritten the way
   the camera would after deleting everything.
5. The card is opened again and must validate with no items left.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

from sony_card_db import DatabaseError, SonyCardDatabase

AVF = "AVF_INFO"
INP, BNP, PRV = "AVIN0001.INP", "AVIN0001.BNP", "PRV00001.BIN"
M4ROOT = os.path.join("PRIVATE", "M4ROOT")
MEDIAPRO = os.path.join(M4ROOT, "MEDIAPRO.XML")
PHOTO_EXTENSIONS = {".ARW", ".JPG", ".JPEG", ".HIF", ".HEIF", ".DNG"}
CHUNK = 4 * 1024 * 1024


class CardError(Exception):
    """The card is not something this tool may change."""


@dataclass
class CardFile:
    card_path: str  # relative to the card root, with backslashes
    size: int
    modified: dt.datetime
    kind: str  # "photo" or "video"
    database_path: str  # as in SonyCardDatabase items
    copy_with: list[str] = field(default_factory=list)  # sidecars copied too
    delete_with: list[str] = field(default_factory=list)  # sidecars removed from the card


@dataclass
class Card:
    root: str  # e.g. "\\\\?\\Volume{...}\\" or "G:\\"
    label: str
    files: list[CardFile]
    database: SonyCardDatabase

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class MoveResult:
    copied: list[str]  # destination paths
    already_there: list[str]
    backup_folder: str


Progress = Callable[[str, int, int], None]  # message key, done bytes, total bytes


# --------------------------------------------------------------- discovery
def removable_volumes() -> list[str]:
    """Return root paths of removable volumes, with or without a drive letter."""

    if os.name != "nt":
        return []
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    kernel32.FindFirstVolumeW.restype = wintypes.HANDLE
    kernel32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.FindNextVolumeW.restype = wintypes.BOOL
    kernel32.FindVolumeClose.argtypes = [wintypes.HANDLE]
    kernel32.FindVolumeClose.restype = wintypes.BOOL
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetVolumePathNamesForVolumeNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.GetVolumePathNamesForVolumeNameW.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(1024)
    handle = kernel32.FindFirstVolumeW(buffer, len(buffer))
    if handle in (None, wintypes.HANDLE(-1).value):
        return []
    roots = []
    try:
        while True:
            volume = buffer.value
            if kernel32.GetDriveTypeW(volume) == 2:  # DRIVE_REMOVABLE
                names = ctypes.create_unicode_buffer(1024)
                length = wintypes.DWORD()
                if kernel32.GetVolumePathNamesForVolumeNameW(
                    volume, names, len(names), ctypes.byref(length)
                ) and names.value:
                    roots.append(names.value)  # first drive letter / mount point
                else:
                    roots.append(volume)
            if not kernel32.FindNextVolumeW(handle, buffer, len(buffer)):
                break
    finally:
        kernel32.FindVolumeClose(handle)
    return roots


def looks_like_sony_card(root: str) -> bool:
    avf = os.path.join(root, AVF)
    return all(os.path.isfile(os.path.join(avf, name)) for name in (INP, BNP, PRV)) and (
        os.path.isdir(os.path.join(root, "PRIVATE", "SONY"))
        or os.path.isdir(os.path.join(root, M4ROOT))
    )


def find_sony_cards() -> list[str]:
    return [root for root in removable_volumes() if looks_like_sony_card(root)]


def volume_label(root: str) -> str:
    if os.name != "nt":
        return ""
    name = ctypes.create_unicode_buffer(261)
    if ctypes.windll.kernel32.GetVolumeInformationW(root, name, 261, None, None, None, None, 0):
        return name.value
    return ""


def _read(root: str, *parts: str) -> bytes:
    with open(os.path.join(root, *parts), "rb") as handle:
        return handle.read()


def open_card(root: str) -> Card:
    if not looks_like_sony_card(root):
        raise CardError("not_sony_card")
    try:
        database = SonyCardDatabase(_read(root, AVF, INP), _read(root, AVF, BNP), _read(root, AVF, PRV))
    except DatabaseError as exc:
        raise CardError("database_not_understood", str(exc)) from exc

    on_card: dict[str, str] = {}  # database path (upper) -> card path
    dcim = os.path.join(root, "DCIM")
    if os.path.isdir(dcim):
        for folder in os.listdir(dcim):
            folder_path = os.path.join(dcim, folder)
            if not os.path.isdir(folder_path):
                continue
            for name in os.listdir(folder_path):
                if os.path.splitext(name)[1].upper() in PHOTO_EXTENSIONS:
                    on_card[f"DCIM/{folder}/{name}".upper()] = os.path.join("DCIM", folder, name)
    clip = os.path.join(root, M4ROOT, "CLIP")
    if os.path.isdir(clip):
        for name in os.listdir(clip):
            if name.upper().endswith(".MP4"):
                on_card[f"PRIVATE/M4ROOT/CLIP/{name}".upper()] = os.path.join(M4ROOT, "CLIP", name)

    in_database = {item.path.upper(): item for item in database.items}
    not_indexed = sorted(set(on_card) - set(in_database))
    missing = sorted(set(in_database) - set(on_card))
    if not_indexed or missing:
        raise CardError(
            "card_and_database_differ",
            "; ".join([*(f"+{p}" for p in not_indexed), *(f"-{p}" for p in missing)]),
        )

    files = []
    for key, item in in_database.items():
        card_path = on_card[key]
        stat = os.stat(os.path.join(root, card_path))
        card_file = CardFile(
            card_path=card_path,
            size=stat.st_size,
            modified=dt.datetime.fromtimestamp(stat.st_mtime),
            kind=item.kind,
            database_path=item.path,
        )
        if item.kind == "video":
            stem = os.path.splitext(os.path.basename(card_path))[0]
            for sidecar, copy in (
                (os.path.join(M4ROOT, "CLIP", f"{stem}M01.XML"), True),
                (os.path.join(M4ROOT, "THMBNL", f"{stem}T01.JPG"), False),
                (os.path.join(M4ROOT, "SUB", f"{stem}S03.MP4"), False),
            ):
                if os.path.isfile(os.path.join(root, sidecar)):
                    card_file.delete_with.append(sidecar)
                    if copy:
                        card_file.copy_with.append(sidecar)
        files.append(card_file)
    files.sort(key=lambda f: (f.modified, f.card_path))
    return Card(root=root, label=volume_label(root), files=files, database=database)


# ------------------------------------------------------------------ copying
def _hash_file(path: str, cancel: threading.Event, progress: Callable[[int], None]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            if cancel.is_set():
                raise CardError("cancelled")
            digest.update(chunk)
            progress(len(chunk))
    return digest.hexdigest()


def _unique(path: str) -> str:
    stem, ext = os.path.splitext(path)
    counter = 2
    while os.path.exists(f"{stem} ({counter}){ext}"):
        counter += 1
    return f"{stem} ({counter}){ext}"


def _copy_verified(
    source: str, target: str, cancel: threading.Event, progress: Callable[[int], None]
) -> tuple[str, bool]:
    """Copy and verify; return (final path, whether an identical copy existed)."""

    source_size = os.path.getsize(source)
    if os.path.exists(target) and os.path.getsize(target) == source_size:
        if _hash_file(source, cancel, lambda n: None) == _hash_file(target, cancel, progress):
            return target, True
        target = _unique(target)
    elif os.path.exists(target):
        target = _unique(target)

    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    digest = hashlib.sha256()
    try:
        with open(source, "rb") as reader, open(part, "wb") as writer:
            while chunk := reader.read(CHUNK):
                if cancel.is_set():
                    raise CardError("cancelled")
                digest.update(chunk)
                writer.write(chunk)
                progress(len(chunk) // 2)
            writer.flush()
            os.fsync(writer.fileno())
        if _hash_file(part, cancel, lambda n: progress(n // 2)) != digest.hexdigest():
            raise CardError("copy_mismatch", source)
        stat = os.stat(source)
        os.utime(part, (stat.st_atime, stat.st_mtime))
        os.replace(part, target)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return target, False


def destination_for(name: str, taken: dt.datetime, destination: str, by_date: bool) -> str:
    folder = os.path.join(destination, taken.strftime("%Y-%m-%d")) if by_date else destination
    return os.path.join(folder, name)


# --------------------------------------------------------- card rewriting
def _rewrite_mediapro(data: bytes, removed: set[str], moment: dt.datetime) -> bytes:
    """Drop the <Material> blocks of removed clips; keep the fixed file size."""

    text = data.decode("utf-8")
    end = text.find("</MediaProfile>")
    if end < 0:
        raise CardError("database_not_understood", "MEDIAPRO.XML")
    xml = text[: end + len("</MediaProfile>")]
    for name in removed:
        pattern = re.compile(
            r"\t\t<Material uri=\"\./CLIP/" + re.escape(name) + r"\".*?</Material>\n", re.S
        )
        xml, count = pattern.subn("", xml)
        if count != 1:
            raise CardError("database_not_understood", f"MEDIAPRO.XML entry for {name}")
    xml = re.sub(r"\t<Contents>\n\t</Contents>\n", "", xml)
    # Write the time in the camera's own time zone, taken from the file.
    zone = re.search(r'createdAt="[^"]*([+-])(\d\d):(\d\d)"', xml)
    if zone:
        sign = 1 if zone.group(1) == "+" else -1
        offset = dt.timedelta(hours=int(zone.group(2)), minutes=int(zone.group(3)))
        moment = moment.astimezone(dt.timezone(sign * offset))
    stamp = moment.isoformat(timespec="seconds")
    xml = re.sub(r'createdAt="[^"]*"', f'createdAt="{stamp}"', xml, count=1)
    body = xml.encode("utf-8")
    if len(body) + 1 > len(data):
        raise CardError("database_not_understood", "MEDIAPRO.XML would grow")
    return body + b" " * (len(data) - len(body) - 1) + b"\n"


def _write_in_place(path: str, data: bytes) -> None:
    """Overwrite an existing file of the same size, keeping its attributes."""

    if os.path.getsize(path) != len(data):
        raise CardError("database_not_understood", f"size of {os.path.basename(path)}")
    with open(path, "r+b") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_empty(root: str, folder: str) -> None:
    path = os.path.join(root, folder)
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        child = os.path.join(folder, name)
        if os.path.isdir(os.path.join(root, child)):
            _remove_empty(root, child)
    if not os.listdir(path):
        os.rmdir(path)


def backup_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "CameraCardMover", "card-database-backups")


def move_all(
    card: Card,
    destination: str,
    *,
    by_date: bool,
    progress: Progress,
    cancel: threading.Event,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now().astimezone(),
) -> MoveResult:
    root = card.root
    total = sum(f.size + sum(os.path.getsize(os.path.join(root, s)) for s in f.copy_with) for f in card.files)
    done = 0

    def advance(count: int) -> None:
        nonlocal done
        done += count
        progress("copying", done, total)

    copied, already = [], []
    for card_file in card.files:
        for relative in [card_file.card_path, *card_file.copy_with]:
            target = destination_for(os.path.basename(relative), card_file.modified, destination, by_date)
            target, existed = _copy_verified(os.path.join(root, relative), target, cancel, advance)
            (already if existed else copied).append(target)

    if cancel.is_set():
        raise CardError("cancelled")
    progress("updating_card", total, total)

    # Re-read the card: nothing may have changed since it was opened.
    current = open_card(root)
    if [f.card_path for f in current.files] != [f.card_path for f in card.files]:
        raise CardError("card_changed")

    moment = now()
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(backup_root(), stamp)
    os.makedirs(backup, exist_ok=True)
    originals = {name: _read(root, AVF, name) for name in (INP, BNP, PRV)}
    for name, data in originals.items():
        with open(os.path.join(backup, name), "wb") as handle:
            handle.write(data)
    mediapro_path = os.path.join(root, MEDIAPRO)
    mediapro = _read(root, MEDIAPRO) if os.path.isfile(mediapro_path) else None
    if mediapro is not None:
        with open(os.path.join(backup, "MEDIAPRO.XML"), "wb") as handle:
            handle.write(mediapro)

    database = current.database
    database.delete([f.database_path for f in current.files], moment)
    new_inp, new_bnp, new_prv = database.to_bytes()
    videos = {os.path.basename(f.card_path) for f in current.files if f.kind == "video"}
    new_mediapro = _rewrite_mediapro(mediapro, videos, moment) if videos and mediapro else None

    for card_file in current.files:
        for relative in [card_file.card_path, *card_file.delete_with]:
            os.remove(os.path.join(root, relative))
    _write_in_place(os.path.join(root, AVF, INP), new_inp)
    _write_in_place(os.path.join(root, AVF, BNP), new_bnp)
    _write_in_place(os.path.join(root, AVF, PRV), new_prv)
    if new_mediapro is not None:
        _write_in_place(mediapro_path, new_mediapro)
    for folder in ("DCIM", "MP_ROOT"):
        _remove_empty(root, folder)

    after = open_card(root)
    if after.files:
        raise CardError("card_not_empty_after_move")
    return MoveResult(copied=copied, already_there=already, backup_folder=backup)

