"""Camera transfer over Wi-Fi, independent of any user interface.

``WifiTransfer`` runs the Sony direct (XPush) client or the airMTP PTP/IP
backend in a worker thread and reports progress as ``(event, payload)``
tuples on a queue; texts in the events are already translated.  Events:
``log`` (text), ``status`` (text), ``storage`` (total, free, label),
``overall`` (current, total), ``file_progress`` (name, percent, current,
total), ``attention`` (None: the user has to act on the camera) and
``done`` ({success, downloaded, skipped}).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend_output import BackendOutputParser
from localization import Translator
from sony_soap import SonySoapClient, SonySoapError, TransportError
from windows_pairing import PairingError, pair_camera


APP_NAME = "Camera Wi-Fi Transfer"
APP_VERSION = "0.4.0-beta"
PAIRING_GUID = "0xa9fdb6005452ffff-0x3aa907283c52ffff"
IMAGE_EXTENSIONS = ["JPG", "JPEG", "JPE", "HEIF", "HIF"]
RAW_EXTENSIONS = [
    "3FR", "ARW", "CR2", "CR3", "DNG", "ERF", "IIQ", "KDC", "MEF", "MOS",
    "MRW", "NEF", "NRW", "ORF", "ORI", "PEF", "RAF", "RAW", "RW2", "RWL",
    "SR2", "SRW", "X3F",
]
VIDEO_EXTENSIONS = ["AVI", "M2TS", "MOV", "MP4", "MTS", "MXF", "THM", "XML"]
# When the transfer starts automatically the camera may still be switching
# into "Send to Computer", so keep looking for it this long.
CAMERA_WAIT_SECONDS = 5 * 60
CAMERA_RETRY_PAUSE_SECONDS = 5
# airMTP exit code for "camera not found / could not connect".
BACKEND_ERRNO_COULDNT_CONNECT = 4004
# airMTP exits with codes in this range for problems a retry cannot fix
# (bad arguments, local file errors, ...).
BACKEND_ERRNO_DONT_RETRY = range(5000, 5100)
# airMTP lines hidden in the GUI: its start-up banner (printed again on
# every reconnect, with a new time stamp each time) and its advice after a
# broken Sony session, which the GUI replaces with a localized message
# that also says the app will continue by itself.
BACKEND_NOISE_PREFIXES = (
    "airmtp v",
    "Time: ",
    "Before running airmtp again",
    "'Send to Computer' screen and select",
    "do this will cause the next airmtp session",
    "Number of attempts (",
)


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent


def user_home_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home())))


def known_user_folder(registry_name: str, fallback: Path) -> Path:
    """Return a redirected Windows user folder when Explorer defines one."""

    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, registry_name)
            if isinstance(value, str) and value:
                return Path(os.path.expandvars(value))
        except (OSError, ValueError):
            pass
    return fallback


def desktop_dir() -> Path:
    return known_user_folder("Desktop", user_home_dir() / "Desktop")


def pictures_dir() -> Path:
    return known_user_folder("My Pictures", user_home_dir() / "Pictures")


def default_pictures_dir() -> Path:
    return pictures_dir() / "Camera Imports"


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(executable_dir())))
    return base / "CameraWiFiTransfer"


def legacy_app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(executable_dir())))
    return base / "A6500WiFiCopy"


def primary_lan_ipv4() -> str | None:
    """Return the IPv4 address selected by Windows' default route.

    Sony discovery is multicast and therefore must use the real LAN adapter.
    Resolving the PC hostname is unreliable on machines with VirtualBox or
    Hyper-V adapters.  A UDP ``connect`` performs only local route selection;
    it does not send a packet.
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    if not address or address == "0.0.0.0" or address.startswith("127."):
        return None
    return address


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_elevated_pairing(result_path: Path, language: str) -> int:
    """Run only the native USB operation elevated and wait for its result."""

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    executable = Path(sys.executable).resolve()
    child_args: list[str] = []
    if not getattr(sys, "frozen", False):
        # The running main script handles --pair-helper.
        child_args.append(str(Path(sys.argv[0]).resolve()))
    child_args.extend(
        ["--pair-helper", "--result-file", str(result_path), "--language", language]
    )

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline(child_args)
    info.lpDirectory = str(executable_dir())
    info.nShow = 0  # SW_HIDE; the UAC confirmation itself remains visible

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            raise PairingError(Translator(language)("uac_cancelled"))
        raise OSError(error, ctypes.FormatError(error))
    try:
        wait_result = kernel32.WaitForSingleObject(info.hProcess, 60_000)
        if wait_result != 0:
            raise PairingError(Translator(language)("pair_timeout"))
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def pairing_child(result_path: Path, language: str = "auto") -> int:
    """Elevated, UI-free child used exclusively by ``run_elevated_pairing``."""

    expected_directory = Path(tempfile.gettempdir()).resolve()
    resolved = result_path.resolve()
    if (
        resolved.parent != expected_directory
        or not resolved.name.startswith("camera-pair-")
        or resolved.suffix.lower() != ".json"
    ):
        return 2

    try:
        result: dict[str, Any] = pair_camera()
        result["message"] = Translator(language)(
            "pairing_success_message",
            vendor=result["vendor"],
            product=result["product"],
            drive=result["drive"],
        )
        exit_code = 0
    except PairingError as exc:
        result = {"success": False, "message": str(exc)}
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - preserve diagnostics from elevated child
        result = {"success": False, "message": f"{type(exc).__name__}: {exc}"}
        exit_code = 1

    temporary = resolved.with_suffix(".tmp")
    # ASCII escaping keeps the elevated child result independent of the
    # active Windows console code page; json.loads restores the Russian text.
    temporary.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(resolved)
    return exit_code


def backend_command() -> list[str]:
    bundled_backend = executable_dir() / "airmtp-backend.exe"
    if bundled_backend.exists():
        return [str(bundled_backend)]

    source_backend = resource_dir() / "vendor" / "airmtp" / "airmtp.py"
    if source_backend.exists():
        return [sys.executable, str(source_backend)]

    return []


class Settings:
    SETTINGS_VERSION = 4
    DEFAULTS: dict[str, Any] = {
        "destination": str(default_pictures_dir()),
        "mode": "ptpip",
        "camera_ip": "",
        "include_jpeg": True,
        "include_raw": True,
        "include_video": False,
        "organize_by_date": True,
        "pairing_profile": "helper",
        "language": "auto",
        "notify_on_complete": True,
        "auto_start": True,
        "settings_version": SETTINGS_VERSION,
    }

    def __init__(self) -> None:
        self.path = app_data_dir() / "settings.json"
        self.legacy_path = legacy_app_data_dir() / "settings.json"
        self.values = dict(self.DEFAULTS)

    def load(self) -> dict[str, Any]:
        try:
            source_path = self.path if self.path.exists() else self.legacy_path
            loaded = json.loads(source_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in self.DEFAULTS:
                    if key in loaded:
                        self.values[key] = loaded[key]

                # Preserve every user choice from the a6500 prototype.  Only
                # newly introduced options receive defaults.
                self.values["settings_version"] = self.SETTINGS_VERSION
        except (OSError, ValueError, TypeError):
            pass
        return self.values

    def save(self, values: dict[str, Any]) -> None:
        self.values.update(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self.values, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            # Settings are convenient, but never worth aborting a photo transfer.
            pass




@dataclass
class TransferOptions:
    mode: str = "ptpip"  # "ptpip" (home network) or "direct" (Sony XPush)
    destination: str = ""
    camera_ip: str = ""
    include_jpeg: bool = True
    include_raw: bool = True
    include_video: bool = False
    organize_by_date: bool = True
    pairing_profile: str = "helper"

    @classmethod
    def from_settings(cls, values: dict[str, Any]) -> "TransferOptions":
        return cls(**{name: values[name] for name in cls.__dataclass_fields__ if name in values})


class WifiTransfer:
    def __init__(self, translator: Translator, events: "queue.Queue[tuple[str, Any]]") -> None:
        self.t = translator
        self.events = events
        self.options = TransferOptions()
        self.worker: threading.Thread | None = None
        self.backend_process: subprocess.Popen[str] | None = None
        self.cancel_requested = threading.Event()

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start(self, options: TransferOptions, *, wait_for_camera: bool = False) -> None:
        """Start a transfer; the caller has checked the destination and file types."""

        if self.running:
            return
        self.options = options
        self.cancel_requested.clear()
        destination = Path(options.destination).expanduser()
        deadline = time.monotonic() + CAMERA_WAIT_SECONDS if wait_for_camera else None
        target = self._direct_worker if options.mode == "direct" else self._ptpip_worker
        self.worker = threading.Thread(target=target, args=(destination, deadline), daemon=True)
        self.worker.start()

    def stop(self) -> None:
        # Set the flag first so a worker that is waiting to retry the camera
        # search stops instead of starting another attempt.
        self.cancel_requested.set()
        process = self.backend_process
        if process and process.poll() is None:
            process.terminate()
        key = "stopped_network" if self.options.mode == "ptpip" else "stopping_direct"
        self.events.put(("log", self.t(key)))

    def _pause_before_camera_retry(
        self,
        deadline: float | None,
        attempt: int,
        *,
        log_key: str = "camera_wait_log",
        status_key: str = "camera_wait_status",
        expired_key: str = "camera_wait_expired",
    ) -> bool:
        """Wait before searching for the camera again.

        Returns False when there is no wait window, it has run out, or the
        user pressed Stop.  Explains the wait once in the log (``attempt``
        1); afterwards only the status line changes so the log stays
        readable.
        """

        if deadline is None or self.cancel_requested.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= CAMERA_RETRY_PAUSE_SECONDS:
            self.events.put(("log", self.t(expired_key, minutes=CAMERA_WAIT_SECONDS // 60)))
            return False
        if attempt == 1:
            self.events.put(("log", self.t(log_key, minutes=CAMERA_WAIT_SECONDS // 60)))
            # The user has to act on the camera, so bring the window up.
            self.events.put(("attention", None))
        minutes_left = max(1, int(remaining + 59) // 60)
        self.events.put(("status", self.t(status_key, minutes=minutes_left)))
        return not self.cancel_requested.wait(CAMERA_RETRY_PAUSE_SECONDS)

    def _direct_worker(self, destination: Path, deadline: float | None = None) -> None:
        client = SonySoapClient()
        downloaded = 0
        skipped = 0
        session_started = False
        success = False
        try:
            self.events.put(("log", self.t("looking_direct")))
            attempt = 0
            while True:
                attempt += 1
                try:
                    descriptor = client.probe()
                    break
                except TransportError:
                    if not self._pause_before_camera_retry(deadline, attempt):
                        raise
            self.events.put(("status", self.t("camera_found")))
            camera_name = descriptor.model_name or descriptor.friendly_name or "Sony"
            self.events.put(("log", self.t("camera_found_reading", camera=camera_name)))
            client.start_transfer()
            session_started = True
            items = client.browse_all()
            self.events.put(("log", self.t("files_available", count=len(items))))

            for index, item in enumerate(items, start=1):
                if self.cancel_requested.is_set():
                    self.events.put(("log", self.t("copy_stopped")))
                    break

                filename = item.title or f"photo-{index}.jpg"

                self.events.put(
                    ("status", self.t("downloading_count", current=index, total=len(items)))
                )
                self.events.put(("overall", (index - 1, len(items))))
                self.events.put(("log", self.t("checking_file", filename=filename)))
                received_data = False

                def progress(received: int, total: int | None = None) -> None:
                    nonlocal received_data
                    received_data = True
                    if total:
                        percent = min(100, int(received * 100 / total))
                        self.events.put(
                            ("status", self.t("downloading_file", filename=filename, percent=percent))
                        )
                        self.events.put(("file_progress", (filename, percent, index, len(items))))

                saved_path = client.download(item, destination, progress_cb=progress)
                if received_data:
                    downloaded += 1
                    self.events.put(("log", self.t("saved_file", filename=saved_path.name)))
                else:
                    skipped += 1
                    self.events.put(("log", self.t("already_exists", filename=saved_path.name)))
                self.events.put(("overall", (index, len(items))))

            success = not self.cancel_requested.is_set()
            if success:
                self.events.put(
                    ("log", self.t("done_counts", downloaded=downloaded, skipped=skipped))
                )
        except SonySoapError as exc:
            if self.cancel_requested.is_set():
                self.events.put(("log", self.t("copy_stopped")))
            else:
                self.events.put(("log", self.t("camera_error", error=exc)))
        except Exception as exc:  # noqa: BLE001 - display actionable failure in beta UI
            self.events.put(("log", self.t("failed", error=exc)))
        finally:
            if session_started:
                try:
                    # Sony's reference clients close the push session with
                    # ErrCode 0 even when the local copy was cancelled.
                    client.end_transfer(0)
                except SonySoapError as exc:
                    self.events.put(("log", self.t("session_close_failed", error=exc)))
            self.events.put(
                (
                    "done",
                    {"success": success, "downloaded": downloaded, "skipped": skipped},
                )
            )

    def _ptpip_worker(self, destination: Path, deadline: float | None = None) -> None:
        command = backend_command()
        if not command:
            self.events.put(("log", self.t("backend_missing")))
            self.events.put(("done", False))
            return

        extensions: list[str] = []
        if self.options.include_jpeg:
            extensions.extend(IMAGE_EXTENSIONS)
        if self.options.include_raw:
            extensions.extend(RAW_EXTENSIONS)
        if self.options.include_video:
            extensions.extend(VIDEO_EXTENSIONS)

        camera_address = self.options.camera_ip.strip() or "auto"

        command.extend(
            [
                "--ipaddress",
                camera_address,
                "--action",
                "getfiles",
                "--outputdir",
                str(destination),
                "--ifexists",
                "skip",
                "--downloadhistory",
                # "skipfiles" trusts a persistent per-camera ledger of filenames
                # ever downloaded, even if that file no longer exists in the
                # destination folder (deleted, moved, or synced elsewhere) -
                # this is what silently "skipped" real photos before. Instead
                # rely solely on --ifexists, which skips a file only when it
                # is actually still present in the destination folder.
                "ignore",
                "--cameratransferlist",
                "ignore",
                "--transferorder",
                "newestfirst",
                # airMTP caches file names and sizes by MTP object handle
                # between sessions and only re-checks folder timestamps.
                # That was designed for Nikon/Canon; on Sony, deleting files
                # in the camera can leave a handle pointing at a different
                # file, so a request for cached "C0001.MP4" hung forever.
                # Reading the list fresh costs well under a second here.
                "--mtpobjcache",
                "disabled",
                # Wi-Fi can stall for several seconds; the airMTP default
                # of 5 s aborted whole transfers on a single hiccup.
                "--socketreadwritetimeout",
                "20",
                "--retrycount",
                "1",
                "--ssdp_discoveryattempts",
                "10",
                "--ssdp_discoverytimeoutsecsperattempt",
                "2",
                "--camerasleepwhendone",
                "yes",
                "--logginglevel",
                "normal",
                "--extlist",
                *extensions,
            ]
        )
        lan_address = primary_lan_ipv4() if camera_address == "auto" else None
        if lan_address:
            # Disable airMTP's hostname-based Windows workaround: on this PC
            # it resolves to a VirtualBox adapter.  Use the adapter selected by
            # the real default route instead.
            command.extend(
                ["--ssdp_discoveryflags", "0", "--ssdp_addmulticastif", lan_address]
            )
            self.events.put(("log", self.t("pc_network", address=lan_address)))
        elif camera_address == "auto":
            self.events.put(
                ("log", self.t("network_auto_failed"))
            )
        if self.options.organize_by_date:
            command.extend(["--dirnamespec", "@capturedate@"])
        if self.options.pairing_profile == "helper":
            command.extend(
                ["--initcmdreq_guid", PAIRING_GUID, "--initcmdreq_hostname", "camera-transfer"]
            )

        downloaded = 0
        skipped = 0
        success = False
        shown_log_lines: set[str] = set()
        attempt = 0
        # Files downloaded by earlier, interrupted attempts.  A later attempt
        # finds them in the folder and reports them as skipped, so they are
        # moved back from "skipped" to "downloaded" in the totals.
        earlier_downloads = 0
        reconnect_deadline: float | None = None
        reconnect_attempt = 0
        gave_up_reconnecting = False
        try:
            while True:
                attempt += 1
                exit_code, run_downloaded, run_skipped, total = self._run_backend_once(
                    command, shown_log_lines
                )
                downloaded = earlier_downloads + run_downloaded
                skipped = max(0, run_skipped - earlier_downloads)
                success = exit_code == 0
                if success or self.cancel_requested.is_set():
                    break
                if exit_code in BACKEND_ERRNO_DONT_RETRY:
                    break
                earlier_downloads = downloaded
                if run_downloaded or run_skipped or total or reconnect_deadline is not None:
                    # The camera was reached, then the connection broke (for
                    # example a Wi-Fi drop in the middle of a file).  A Sony
                    # camera then stays on its "Send to Computer" screen and
                    # ignores new sessions until Cancel is pressed on it, so
                    # tell the user and keep reconnecting; files that already
                    # reached the folder are skipped by the next session.
                    if run_downloaded or reconnect_deadline is None:
                        reconnect_deadline = time.monotonic() + CAMERA_WAIT_SECONDS
                        reconnect_attempt = 0
                    reconnect_attempt += 1
                    if not self._pause_before_camera_retry(
                        reconnect_deadline,
                        reconnect_attempt,
                        log_key="connection_lost_log",
                        status_key="reconnect_wait_status",
                        expired_key="reconnect_expired",
                    ):
                        gave_up_reconnecting = not self.cancel_requested.is_set()
                        break
                elif exit_code != BACKEND_ERRNO_COULDNT_CONNECT or not (
                    self._pause_before_camera_retry(deadline, attempt)
                ):
                    break
            if self.cancel_requested.is_set():
                success = False
            elif not success and not gave_up_reconnecting:
                # reconnect_expired already told the user what to do.
                self.events.put(("log", self.t("transfer_incomplete")))
        except OSError as exc:
            self.events.put(("log", self.t("backend_start_failed", error=exc)))
        finally:
            self.backend_process = None
            self.events.put(
                (
                    "done",
                    {"success": success, "downloaded": downloaded, "skipped": skipped},
                )
            )

    def _run_backend_once(
        self, command: list[str], shown_log_lines: set[str]
    ) -> tuple[int, int, int, int]:
        """Run one airMTP session; return (exit code, downloaded, skipped, total).

        ``shown_log_lines`` is shared across retries so repeated searches for
        the camera do not print the same backend messages again.
        """

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        backend_environment = os.environ.copy()
        backend_environment["CAMERA_WIFI_TRANSFER_GUI"] = "1"
        if self.cancel_requested.is_set():
            return 1, 0, 0, 0
        self.backend_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
            env=backend_environment,
        )
        if self.cancel_requested.is_set():
            # Stop was pressed while the process was being created.
            self.backend_process.terminate()
        assert self.backend_process.stdout is not None
        parser = BackendOutputParser()
        current = 0
        total = 0
        downloaded = 0
        skipped = 0
        for raw_line in self.backend_process.stdout:
            parsed = parser.parse(raw_line)
            if parsed.storage_total is not None and parsed.storage_free is not None:
                self.events.put(
                    (
                        "storage",
                        (parsed.storage_total, parsed.storage_free, parsed.storage_label or ""),
                    )
                )
            if parsed.total is not None:
                total = parsed.total
                if parsed.current is not None:
                    current = parsed.current
                self.events.put(("overall", (current if parsed.item_done else max(0, current - 1), total)))
            if parsed.filename is not None:
                if parsed.percent is None:
                    status = self.t("downloading_file_no_percent", filename=parsed.filename)
                else:
                    status = self.t(
                        "downloading_file", filename=parsed.filename, percent=parsed.percent
                    )
                self.events.put(("status", status))
                self.events.put(
                    (
                        "file_progress",
                        (parsed.filename, parsed.percent or 0, current, total),
                    )
                )
            if parsed.item_done:
                if parsed.item_skipped:
                    skipped += 1
                else:
                    downloaded += 1
                    self.events.put(("log", self.t("saved_file", filename=parsed.filename)))
                self.events.put(("overall", (current, total)))
            if parsed.log_text:
                if parsed.log_text.lstrip().startswith(BACKEND_NOISE_PREFIXES):
                    continue
                if parsed.filename is None and parsed.log_text not in shown_log_lines:
                    shown_log_lines.add(parsed.log_text)
                    self.events.put(("log", self._friendly_backend_line(parsed.log_text)))
                if parsed.log_text.startswith("Found camera"):
                    self.events.put(("status", self.t("camera_found")))
        exit_code = self.backend_process.wait()
        self.backend_process = None
        return exit_code, downloaded, skipped, total

    def _friendly_backend_line(self, line: str) -> str:
        replacements = (
            ("Searching for a Sony or Canon camera via SSDP", self.t("searching_network")),
            ("Found camera at IP address", self.t("camera_ip_found")),
            ("No camera found via SSDP Discovery.", self.t("camera_not_found")),
            ("files downloaded", self.t("files_downloaded")),
            ("previously-downloaded files skipped", self.t("files_skipped")),
            (
                "files skipped because they already existed in output directory",
                self.t("files_skipped_existing"),
            ),
        )
        result = line
        for source, target in replacements:
            result = result.replace(source, target)
        return result
