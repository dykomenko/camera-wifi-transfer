"""One open "Send to Computer" session: list, preview and pick what to copy.

The airMTP command-line program runs a whole transfer per process, and a
Sony camera answers only one MTP session per "Send to Computer": after
that it has to be cancelled on the camera.  Browsing the card, letting
the user pick photos and then copying them therefore has to happen inside
one session.  ``CameraSession`` drives airMTP's own, field-tested
functions (connection set-up, Sony commands, object info, partial-object
downloads) step by step and keeps the session alive while the user
chooses.  It has no user interface and is shared by the desktop tests and
the Android app.

The camera accepts the client because it presents the same GUID that the
one-time USB pairing registered for this PC (``PAIRING_GUID``), so a phone
running this code is "the paired PC" to the camera.
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

AIRMTP_DIR = Path(__file__).resolve().parent / "vendor" / "airmtp"
if str(AIRMTP_DIR) not in sys.path:
    sys.path.insert(0, str(AIRMTP_DIR))

import airmtp as A  # noqa: E402
import applog  # noqa: E402
import mtpwifi  # noqa: E402
from mtpdef import (  # noqa: E402
    MTP_OBJASSOC_GenericFolder,
    MTP_OP_GetDeviceInfo,
    MTP_OP_GetPartialObject,
    MTP_OP_GetThumb,
)

# The GUID written into the camera by the one-time USB pairing (see
# windows_pairing.py) and presented by the desktop app.
PAIRING_GUID = "0xa9fdb6005452ffff-0x3aa907283c52ffff"
KEEPALIVE_SECONDS = 4.0
CHUNK_BYTES = 1024 * 1024

Progress = Callable[[int, int], None]  # bytes done, bytes total


class SessionError(Exception):
    """The camera could not be reached or the session broke."""


@dataclass(frozen=True)
class CameraFile:
    handle: int
    name: str
    size: int
    captured: str  # "YYYYMMDDTHHMMSS" as reported by the camera
    format: int

    @property
    def is_raw(self) -> bool:
        return self.name.upper().endswith((".ARW", ".CR2", ".CR3", ".NEF", ".DNG", ".RAF", ".ORF", ".RW2"))

    @property
    def is_video(self) -> bool:
        return self.name.upper().endswith((".MP4", ".MOV", ".MTS", ".M2TS", ".AVI", ".MXF"))


class CameraSession:
    def __init__(
        self,
        *,
        ip_address: str = "auto",
        multicast_interface: str | None = None,
        guid: str = PAIRING_GUID,
        hostname: str = "camera-transfer",
        work_dir: str | None = None,
        discovery_attempts: int = 10,
    ) -> None:
        self.ip_address = ip_address
        self.multicast_interface = multicast_interface
        self.guid = guid
        self.hostname = hostname
        self.work_dir = Path(work_dir or Path(tempfile.gettempdir()) / "camera-session")
        self.discovery_attempts = discovery_attempts
        self.lock = threading.RLock()
        self.connected = False
        self.camera_model = ""
        self._last_keepalive: float | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None

    # ----------------------------------------------------------- setup
    def _configure_airmtp(self) -> None:
        """Initialise airMTP's globals exactly as its own main() would."""

        self.work_dir.mkdir(parents=True, exist_ok=True)
        _close_logs()
        arguments = [
            str(self.work_dir / "airmtp.py"),  # its app-data folder is derived from argv[0]
            "--ipaddress", self.ip_address,
            "--initcmdreq_guid", self.guid,
            "--initcmdreq_hostname", self.hostname,
            "--socketreadwritetimeout", "20",
            "--ssdp_discoveryattempts", str(self.discovery_attempts),
            "--ssdp_discoverytimeoutsecsperattempt", "2",
            "--mtpobjcache", "disabled",
            "--camerasleepwhendone", "yes",
            "--logginglevel", "normal",
        ]
        if self.multicast_interface:
            arguments += ["--ssdp_discoveryflags", "0", "--ssdp_addmulticastif", self.multicast_interface]
        saved = sys.argv
        sys.argv = arguments
        try:
            A.establishAppEnvironment()
            A.applog_init(
                A.APPLOGF_LEVEL_INFORMATIONAL | A.APPLOGF_LEVEL_ERROR,
                os.path.join(A.g.appDataDir, "session-log-last.txt"),
                os.path.join(A.g.appDataDir, "session-log-lifetime.txt"),
            )
            A.processCmdLine()
        finally:
            sys.argv = saved

    def connect(self) -> None:
        """Find the camera, open the MTP session and select the memory card."""

        with self.lock:
            self._configure_airmtp()
            try:
                A.startMtpSession()
                self.connected = True
                A.selectMtpStorageId()
                A.getMtpStorageInfo()
                A.notifyCameraUserTransferSessionStarting()
            except SystemExit as exc:
                self._abandon()
                raise SessionError(f"airmtp exit {exc.code}") from exc
            except Exception as exc:  # noqa: BLE001 - socket, SSDP and MTP errors alike
                self._abandon()
                raise SessionError(str(exc)) from exc
            self.camera_model = getattr(A.g.mtpDeviceInfo, "modelStr", "") or ""
            self._last_keepalive = time.time()
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

    def _keepalive_loop(self) -> None:
        # Sony closes an idle session after some seconds; airMTP keeps it open
        # with a GetDeviceInfo every few seconds, so do the same while idle.
        while not self._keepalive_stop.wait(1.0):
            if not self.lock.acquire(blocking=False):
                continue  # a transfer is running, which keeps the session busy anyway
            try:
                if self.connected and time.time() - (self._last_keepalive or 0) >= KEEPALIVE_SECONDS:
                    mtpwifi.execMtpOp(A.g.socketPrimary, MTP_OP_GetDeviceInfo)
                    self._last_keepalive = time.time()
            except Exception:  # noqa: BLE001 - reported by the next real call
                self.connected = False
            finally:
                self.lock.release()

    def _touch(self) -> None:
        self._last_keepalive = time.time()

    # ---------------------------------------------------------- content
    def storage(self) -> tuple[int, int, str]:
        """(capacity, free bytes, volume label) of the first card."""

        info = A.g.mtpStorageInfoList[0]
        return info.maxCapacityBytes, info.freeSpaceBytes, info.volumeLabel or ""

    def list_files(self) -> list[CameraFile]:
        """Every file on the selected card, newest first."""

        with self.lock:
            self._require()
            try:
                handles = A.getMtpObjectHandles(A.g.storageId)
                files = []
                for handle in handles:
                    info = A.getMtpObjectInfo(handle)
                    self._touch()
                    if info.associationType == MTP_OBJASSOC_GenericFolder:
                        continue
                    files.append(
                        CameraFile(handle, info.filename, int(info.objectCompressedSize), info.captureDateStr, info.objectFormat)
                    )
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                raise SessionError(str(exc)) from exc
        files.sort(key=lambda f: (f.captured, f.name), reverse=True)
        return files

    def thumbnail(self, item: CameraFile) -> bytes:
        """The small JPEG preview the camera keeps for a file (a few KB)."""

        with self.lock:
            self._require()
            try:
                result = mtpwifi.execMtpOp(A.g.socketPrimary, MTP_OP_GetThumb, struct.pack("<I", item.handle))
            except mtpwifi.MtpOpExecFailureException:
                self._touch()
                return b""  # some objects have no preview; the session is fine
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                raise SessionError(str(exc)) from exc
            self._touch()
            return bytes(result.dataReceived)

    def download(
        self,
        item: CameraFile,
        target: str,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        """Copy one file to ``target`` through ``target + '.part'``."""

        part = target + ".part"
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        done = 0
        with self.lock:
            self._require()
            try:
                with open(part, "wb") as output:
                    while done < item.size:
                        if cancel is not None and cancel.is_set():
                            raise SessionError("cancelled")
                        count = min(CHUNK_BYTES, item.size - done)
                        result = mtpwifi.execMtpOp(
                            A.g.socketPrimary, MTP_OP_GetPartialObject, struct.pack("<III", item.handle, done, count)
                        )
                        data = result.dataReceived
                        if not data:
                            raise SessionError(f"no data for {item.name} at {done}")
                        output.write(data)
                        done += len(data)
                        self._touch()
                        if progress is not None:
                            progress(done, item.size)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(part, target)
            except SessionError:
                _remove(part)
                raise
            except Exception as exc:  # noqa: BLE001
                _remove(part)
                self.connected = False
                raise SessionError(str(exc)) from exc
        return target

    # ------------------------------------------------------------ close
    def close(self, *, sleep_camera: bool = True) -> None:
        """End the session; by default send the camera to sleep, which also
        leaves "Send to Computer" so the next session works without Cancel."""

        self._keepalive_stop.set()
        with self.lock:
            if self.connected:
                A.g.args["camerasleepwhendone"] = "yes" if sleep_camera else "no"
                try:
                    A.endMtpSession()
                except Exception:  # noqa: BLE001 - closing a broken session
                    pass
            self._abandon()

    def _abandon(self) -> None:
        self.connected = False
        try:
            A.closeSockets()
        except Exception:  # noqa: BLE001
            pass
        _close_logs()

    def _require(self) -> None:
        if not self.connected:
            raise SessionError("not connected")


def _close_logs() -> None:
    # airMTP's applog_shutdown() closes the session log twice and never the
    # lifetime log, so close both here.
    for name in ("gFileSessionLog", "gFileLifetimeLog"):
        handle = getattr(applog, name, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            setattr(applog, name, None)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------ manual
def _manual_test() -> int:
    """Interactive check against a real camera: list, preview, pick, copy."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", default="auto")
    parser.add_argument("--interface", default=None, help="this PC's LAN address for discovery")
    parser.add_argument("--out", default=str(Path.home() / "Desktop" / "camera-session-test"))
    args = parser.parse_args()
    if args.interface is None and args.ip == "auto":
        from wifi_transfer import primary_lan_ipv4

        args.interface = primary_lan_ipv4()

    session = CameraSession(ip_address=args.ip, multicast_interface=args.interface)
    print("Looking for the camera (turn on Send to Computer)...")
    started = time.time()
    session.connect()
    print(f"Connected to {session.camera_model} in {time.time() - started:.1f} s")
    try:
        files = session.list_files()
        print(f"{len(files)} files on the card")
        thumbs = Path(args.out) / "thumbs"
        thumbs.mkdir(parents=True, exist_ok=True)
        started = time.time()
        for index, item in enumerate(files):
            data = session.thumbnail(item)
            (thumbs / f"{index:03d}_{item.name}.jpg").write_bytes(data)
            print(f"  [{index:3d}] {item.name:14s} {item.size / 2**20:7.1f} MB  {item.captured}  preview {len(data)} B")
        print(f"Previews saved to {thumbs} in {time.time() - started:.1f} s")
        print("The session stays open while you choose (keep-alive).")
        picked = input("Numbers to copy, e.g. 0,3,5 (empty = none): ").strip()
        for number in [int(x) for x in picked.split(",") if x.strip()]:
            item = files[number]
            target = str(Path(args.out) / item.name)
            started = time.time()
            session.download(item, target, lambda d, t: print(f"\r  {item.name}: {d * 100 // t}%", end=""))
            seconds = time.time() - started
            print(f"\r  {item.name}: done, {item.size / 2**20 / max(seconds, 0.01):.1f} MB/s")
    finally:
        session.close()
        print("Session closed, camera sent to sleep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_manual_test())
