from __future__ import annotations

import argparse
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
import uuid
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Any

from backend_output import BackendOutputParser, clean_backend_text
from localization import Translator, resolve_language
from sony_soap import SonySoapClient, SonySoapError
from windows_pairing import PAIRING_CDB, PairingError, build_pairing_payload, pair_camera


APP_NAME = "Camera Wi-Fi Transfer"
APP_VERSION = "0.3.0-beta"
PAIRING_GUID = "0xa9fdb6005452ffff-0x3aa907283c52ffff"
IMAGE_EXTENSIONS = ["JPG", "JPEG", "JPE", "HEIF", "HIF"]
RAW_EXTENSIONS = [
    "3FR", "ARW", "CR2", "CR3", "DNG", "ERF", "IIQ", "KDC", "MEF", "MOS",
    "MRW", "NEF", "NRW", "ORF", "ORI", "PEF", "RAF", "RAW", "RW2", "RWL",
    "SR2", "SRW", "X3F",
]
VIDEO_EXTENSIONS = ["AVI", "M2TS", "MOV", "MP4", "MTS", "MXF", "THM", "XML"]


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
        child_args.append(str(Path(__file__).resolve()))
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


class SetupDialog(tk.Toplevel):
    def __init__(self, owner: "ImportApp") -> None:
        super().__init__(owner)
        self.owner = owner
        self.t = owner.t
        self.title(self.t("pairing_title"))
        self.geometry("650x570")
        self.minsize(610, 520)
        self.transient(owner)
        self.grab_set()

        outer = ttk.Frame(self, padding=22)
        outer.pack(fill=BOTH, expand=True)

        ttk.Label(outer, text=self.t("pairing_heading"), style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=self.t("pairing_intro"),
            wraplength=595,
            style="Muted.TLabel",
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 18))

        ttk.Label(outer, text=self.t("pairing_steps"), wraplength=595, justify=LEFT).pack(anchor="w")

        warning = ttk.Frame(outer, style="Warning.TFrame", padding=12)
        warning.pack(fill=X, pady=(18, 12))
        ttk.Label(
            warning,
            text=self.t("pairing_warning"),
            wraplength=555,
            justify=LEFT,
            style="Warning.TLabel",
        ).pack(anchor="w")

        self.status = tk.StringVar(value=self.t("pairing_ready"))
        ttk.Label(outer, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(6, 12))

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, side="bottom")
        self.run_button = ttk.Button(
            buttons,
            text=self.t("pair_camera"),
            style="Accent.TButton",
            command=self.run_pairing,
        )
        self.run_button.pack(side=LEFT)
        ttk.Button(buttons, text=self.t("close"), command=self.destroy).pack(side=RIGHT)

    def run_pairing(self) -> None:
        approved = messagebox.askyesno(
            self.t("confirm_pairing"),
            self.t("pairing_question"),
            parent=self,
        )
        if not approved:
            return

        self.run_button.configure(state="disabled")
        self.status.set(self.t("waiting_uac"))
        threading.Thread(target=self._pairing_worker, daemon=True).start()

    def _pairing_worker(self) -> None:
        result_path = Path(tempfile.gettempdir()) / f"camera-pair-{uuid.uuid4().hex}.json"
        try:
            exit_code = run_elevated_pairing(result_path, self.owner.language)
            if not result_path.exists():
                raise PairingError(self.t("helper_no_result", code=exit_code))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            success = exit_code == 0 and bool(result.get("success"))
            output = str(result.get("message") or json.dumps(result, ensure_ascii=False))
            self.after(0, self._pairing_done, success, output)
        except Exception as exc:  # noqa: BLE001 - must surface helper failures to the user
            self.after(0, self._pairing_done, False, str(exc))
        finally:
            try:
                result_path.unlink(missing_ok=True)
                result_path.with_suffix(".tmp").unlink(missing_ok=True)
            except OSError:
                pass

    def _pairing_done(self, success: bool, output: str) -> None:
        self.run_button.configure(state="normal")
        if success:
            self.status.set(self.t("pairing_written"))
            self.owner.pairing_profile.set("helper")
            self.owner.save_settings()
            messagebox.showinfo(
                self.t("pairing_success_title"),
                output + "\n\n" + self.t("pairing_success_suffix"),
                parent=self,
            )
        else:
            self.status.set(self.t("pairing_not_confirmed"))
            compact = output[-1800:] if output else self.t("no_output")
            messagebox.showerror(
                self.t("pairing_failed_title"),
                self.t("pairing_failed_text", output=compact),
                parent=self,
            )


class ImportApp(tk.Tk):
    def __init__(
        self,
        *,
        visible: bool = True,
        language_override: str | None = None,
        demo_mode: bool = False,
    ) -> None:
        super().__init__()
        if not visible:
            self.withdraw()

        self.settings = Settings()
        values = self.settings.load()
        self.demo_mode = demo_mode
        if demo_mode:
            values = dict(values)
            values["destination"] = r"C:\Users\Alex\Pictures\Camera Imports"
            values["camera_ip"] = ""
            values["mode"] = "ptpip"
        language_preference = language_override or str(values["language"])
        self.language_preference = tk.StringVar(value=language_preference)
        self.language = resolve_language(self.language_preference.get())
        self.t = Translator(self.language)
        self.language_choice = tk.StringVar()
        self.title(f"{self.t('app_title')}  {APP_VERSION}")
        self.geometry("860x760")
        self.minsize(780, 680)
        self.destination = tk.StringVar(value=str(values["destination"]))
        self.mode = tk.StringVar(value=str(values["mode"]))
        self.camera_ip = tk.StringVar(value=str(values["camera_ip"]))
        self.include_jpeg = tk.BooleanVar(value=bool(values["include_jpeg"]))
        self.include_raw = tk.BooleanVar(value=bool(values["include_raw"]))
        self.include_video = tk.BooleanVar(value=bool(values["include_video"]))
        self.organize_by_date = tk.BooleanVar(value=bool(values["organize_by_date"]))
        self.notify_on_complete = tk.BooleanVar(value=bool(values["notify_on_complete"]))
        self.pairing_profile = tk.StringVar(value=str(values["pairing_profile"]))
        self.status_text = tk.StringVar(value=self.t("ready"))
        self.detail_text = tk.StringVar(value=self.t("waiting_transfer"))
        self.storage_text = tk.StringVar(value=self.t("storage_unknown"))
        self.instruction_text = tk.StringVar()
        self.current_file_percent = 0
        self.overall_current = 0
        self.overall_total = 0
        self.session_downloaded = 0
        self.session_skipped = 0
        self.selected_only = False

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.backend_process: subprocess.Popen[str] | None = None
        self.cancel_requested = threading.Event()

        if visible:
            try:
                Path(self.destination.get()).mkdir(parents=True, exist_ok=True)
            except OSError:
                # The normal Start button will show a useful error if the
                # selected destination cannot be created.
                pass

        self._configure_styles()
        self._build_ui()
        self._update_mode_ui(persist=visible and not demo_mode)
        if visible and demo_mode:
            self.status_text.set(self.t("camera_found"))
            self.storage_text.set(
                self.t(
                    "storage_status",
                    label="SDXC",
                    free="31.8 GB",
                    total="59.6 GB",
                    percent=53,
                )
            )
            self.detail_text.set(
                self.t(
                    "progress_detail",
                    current=12,
                    total=36,
                    overall=33,
                    filename="DSC01248.ARW",
                    file_percent=64,
                )
            )
            self.progress.configure(value=33)
            self._append_log(self.t("saved_file", filename="DSC01247.ARW"))
            self._append_log(self.t("downloading_file_no_percent", filename="DSC01248.ARW"))
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        # Tcl parses a font description as a list.  A family containing a
        # space must be grouped, otherwise it treats "UI" as the point size.
        self.option_add("*Font", "{Segoe UI} 10")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground="#152238")
        style.configure("DialogTitle.TLabel", font=("Segoe UI Semibold", 17), foreground="#152238")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11), foreground="#152238")
        style.configure("Muted.TLabel", foreground="#5d6878")
        style.configure("Hint.TLabel", foreground="#335d92")
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 9))
        style.configure("Secondary.TButton", padding=(13, 9))
        style.configure("Card.TFrame", background="#f6f8fb")
        style.configure("Warning.TFrame", background="#fff4df")
        style.configure("Warning.TLabel", background="#fff4df", foreground="#6b4815")
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 10), foreground="#1f6b45")
        style.configure("Transfer.Horizontal.TProgressbar", thickness=11)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(26, 22))
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=X)
        header_text = ttk.Frame(header)
        header_text.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(header_text, text=self.t("app_title"), style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header_text,
            text=self.t("app_subtitle"),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 18))

        language_frame = ttk.Frame(header)
        language_frame.pack(side=RIGHT, anchor="ne", padx=(12, 0))
        ttk.Label(language_frame, text=self.t("language"), style="Muted.TLabel").pack(anchor="e")
        language_labels = {
            "auto": self.t("language_auto"),
            "ru": self.t("language_ru"),
            "en": self.t("language_en"),
        }
        self.language_label_to_code = {label: code for code, label in language_labels.items()}
        self.language_choice.set(language_labels.get(self.language_preference.get(), language_labels["auto"]))
        self.language_combo = ttk.Combobox(
            language_frame,
            textvariable=self.language_choice,
            values=list(language_labels.values()),
            state="readonly",
            width=11,
        )
        self.language_combo.pack(anchor="e", pady=(3, 0))
        self.language_combo.bind("<<ComboboxSelected>>", self._change_language)

        ttk.Label(outer, text=self.t("transfer_mode"), style="Section.TLabel").pack(anchor="w")
        modes = ttk.Frame(outer)
        modes.pack(fill=X, pady=(7, 14))
        ttk.Radiobutton(
            modes,
            text=self.t("direct_mode"),
            variable=self.mode,
            value="direct",
            command=self._update_mode_ui,
        ).pack(anchor="w")
        ttk.Radiobutton(
            modes,
            text=self.t("network_mode"),
            variable=self.mode,
            value="ptpip",
            command=self._update_mode_ui,
        ).pack(anchor="w", pady=(5, 0))

        instruction_card = ttk.Frame(outer, style="Card.TFrame", padding=13)
        instruction_card.pack(fill=X, pady=(0, 16))
        ttk.Label(
            instruction_card,
            textvariable=self.instruction_text,
            style="Hint.TLabel",
            wraplength=720,
            justify=LEFT,
        ).pack(anchor="w")

        camera_ip_row = ttk.Frame(instruction_card, style="Card.TFrame")
        camera_ip_row.pack(fill=X, pady=(10, 0))
        ttk.Label(camera_ip_row, text=self.t("camera_ip"), style="Hint.TLabel").pack(side=LEFT)
        self.camera_ip_entry = ttk.Entry(camera_ip_row, textvariable=self.camera_ip, width=18)
        self.camera_ip_entry.pack(side=LEFT, padx=(10, 8))
        ttk.Label(camera_ip_row, text=self.t("camera_ip_hint"), style="Muted.TLabel").pack(side=LEFT)

        ttk.Label(outer, text=self.t("destination"), style="Section.TLabel").pack(anchor="w")
        destination_row = ttk.Frame(outer)
        destination_row.pack(fill=X, pady=(7, 14))
        ttk.Entry(destination_row, textvariable=self.destination).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(destination_row, text=self.t("choose"), command=self.choose_destination).pack(side=LEFT, padx=(8, 0))
        ttk.Button(destination_row, text=self.t("open"), command=self.open_destination).pack(side=LEFT, padx=(6, 0))

        self.options_frame = ttk.Frame(outer)
        self.options_frame.pack(fill=X, pady=(0, 14))
        self.jpeg_check = ttk.Checkbutton(self.options_frame, text=self.t("jpeg_heif"), variable=self.include_jpeg)
        self.jpeg_check.pack(side=LEFT)
        self.raw_check = ttk.Checkbutton(self.options_frame, text=self.t("raw"), variable=self.include_raw)
        self.raw_check.pack(side=LEFT, padx=(16, 0))
        self.video_check = ttk.Checkbutton(self.options_frame, text=self.t("video"), variable=self.include_video)
        self.video_check.pack(side=LEFT, padx=(16, 0))
        self.date_check = ttk.Checkbutton(self.options_frame, text=self.t("date_folders"), variable=self.organize_by_date)
        self.date_check.pack(side=LEFT, padx=(22, 0))
        self.notify_check = ttk.Checkbutton(
            self.options_frame,
            text=self.t("notify_complete"),
            variable=self.notify_on_complete,
            command=self.save_settings,
        )
        self.notify_check.pack(side=LEFT, padx=(22, 0))

        action_row = ttk.Frame(outer)
        action_row.pack(fill=X, pady=(0, 14))
        self.start_button = ttk.Button(
            action_row,
            text=self.t("download_new"),
            style="Accent.TButton",
            command=self.start_transfer,
        )
        self.start_button.pack(side=LEFT)
        self.selected_button = ttk.Button(
            action_row,
            text=self.t("download_selected"),
            style="Secondary.TButton",
            command=lambda: self.start_transfer(selected_only=True),
        )
        self.selected_button.pack(side=LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(action_row, text=self.t("stop"), command=self.stop_transfer, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(8, 0))
        ttk.Button(action_row, text=self.t("sony_pairing"), command=self.open_setup).pack(side=RIGHT)

        progress_card = ttk.Frame(outer, style="Card.TFrame", padding=(14, 11))
        progress_card.pack(fill=X)
        status_row = ttk.Frame(progress_card, style="Card.TFrame")
        status_row.pack(fill=X)
        ttk.Label(status_row, textvariable=self.status_text, style="Status.TLabel").pack(side=LEFT)
        ttk.Label(status_row, textvariable=self.storage_text, style="Muted.TLabel").pack(side=RIGHT)
        detail_row = ttk.Frame(progress_card, style="Card.TFrame")
        detail_row.pack(fill=X, pady=(8, 0))
        ttk.Label(
            detail_row,
            textvariable=self.detail_text,
            style="Muted.TLabel",
            width=68,
            anchor="w",
        ).pack(side=LEFT, fill=X, expand=True)
        self.progress = ttk.Progressbar(
            detail_row,
            mode="determinate",
            maximum=100,
            value=0,
            length=190,
            style="Transfer.Horizontal.TProgressbar",
        )
        self.progress.pack(side=RIGHT)

        log_frame = ttk.LabelFrame(outer, text=self.t("copy_progress"), padding=8)
        log_frame.pack(fill=BOTH, expand=True, pady=(10, 0))
        self.log = tk.Text(
            log_frame,
            height=12,
            wrap="word",
            relief="flat",
            background="#f8f9fb",
            foreground="#273444",
            padx=8,
            pady=8,
            state="disabled",
            font=("Segoe UI", 9),
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill="y")
        self.log.pack(fill=BOTH, expand=True)

    def _update_mode_ui(self, *, persist: bool = True) -> None:
        if self.mode.get() == "direct":
            self.instruction_text.set(self.t("direct_instruction"))
            for widget in (self.jpeg_check, self.raw_check, self.video_check, self.date_check):
                widget.configure(state="disabled")
            self.camera_ip_entry.configure(state="disabled")
            self.selected_button.configure(state="disabled")
        else:
            self.instruction_text.set(self.t("network_instruction"))
            for widget in (self.jpeg_check, self.raw_check, self.video_check, self.date_check):
                widget.configure(state="normal")
            self.camera_ip_entry.configure(state="normal")
            self.selected_button.configure(state="normal")
        if persist:
            self.save_settings()

    def _change_language(self, _event: tk.Event[Any] | None = None) -> None:
        if self.worker and self.worker.is_alive():
            return
        preference = self.language_label_to_code.get(self.language_choice.get(), "auto")
        self.language_preference.set(preference)
        self.language = resolve_language(preference)
        self.t = Translator(self.language)
        self.status_text.set(self.t("ready"))
        self.save_settings()
        for child in self.winfo_children():
            child.destroy()
        self.title(f"{self.t('app_title')}  {APP_VERSION}")
        self._configure_styles()
        self._build_ui()
        self._update_mode_ui(persist=False)

    def choose_destination(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.destination.get() or str(default_pictures_dir()))
        if selected:
            self.destination.set(selected)
            self.save_settings()

    def open_destination(self) -> None:
        path = Path(self.destination.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(self.t("error"), str(exc), parent=self)

    def open_setup(self) -> None:
        SetupDialog(self)

    def save_settings(self) -> None:
        if self.demo_mode:
            return
        self.settings.save(
            {
                "destination": self.destination.get(),
                "mode": self.mode.get(),
                "camera_ip": self.camera_ip.get().strip(),
                "include_jpeg": self.include_jpeg.get(),
                "include_raw": self.include_raw.get(),
                "include_video": self.include_video.get(),
                "organize_by_date": self.organize_by_date.get(),
                "notify_on_complete": self.notify_on_complete.get(),
                "pairing_profile": self.pairing_profile.get(),
                "language": self.language_preference.get(),
            }
        )

    def start_transfer(self, selected_only: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            return

        destination = Path(self.destination.get()).expanduser()
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(self.t("cannot_create_folder"), str(exc), parent=self)
            return

        if self.mode.get() == "ptpip" and not any(
            (self.include_jpeg.get(), self.include_raw.get(), self.include_video.get())
        ):
            messagebox.showwarning(
                self.t("no_file_types"), self.t("select_file_types"), parent=self
            )
            return

        self.save_settings()
        if selected_only and self.mode.get() != "ptpip":
            return
        if selected_only and not messagebox.askokcancel(
            self.t("selected_title"),
            self.t("selected_instructions"),
            parent=self,
        ):
            return
        self.selected_only = selected_only
        self.cancel_requested.clear()
        self.start_button.configure(state="disabled")
        self.selected_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_text.set(self.t("connecting"))
        self.detail_text.set(self.t("preparing_transfer"))
        self.storage_text.set(self.t("storage_reading"))
        self.current_file_percent = 0
        self.overall_current = 0
        self.overall_total = 0
        self.session_downloaded = 0
        self.session_skipped = 0
        self._append_log("\n" + self.t("new_run"))

        target = self._direct_worker if self.mode.get() == "direct" else self._ptpip_worker
        self.worker = threading.Thread(target=target, args=(destination,), daemon=True)
        self.worker.start()

    def _direct_worker(self, destination: Path) -> None:
        client = SonySoapClient()
        downloaded = 0
        skipped = 0
        session_started = False
        success = False
        try:
            self.events.put(("log", self.t("looking_direct")))
            descriptor = client.probe()
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

    def _ptpip_worker(self, destination: Path) -> None:
        command = backend_command()
        if not command:
            self.events.put(("log", self.t("backend_missing")))
            self.events.put(("done", False))
            return

        extensions: list[str] = []
        if self.include_jpeg.get():
            extensions.extend(IMAGE_EXTENSIONS)
        if self.include_raw.get():
            extensions.extend(RAW_EXTENSIONS)
        if self.include_video.get():
            extensions.extend(VIDEO_EXTENSIONS)

        camera_address = self.camera_ip.get().strip() or "auto"

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
                "skipfiles",
                "--cameratransferlist",
                "exitifnotavail" if self.selected_only else "ignore",
                "--transferorder",
                "newestfirst",
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
        if self.organize_by_date.get():
            command.extend(["--dirnamespec", "@capturedate@"])
        if self.pairing_profile.get() == "helper":
            command.extend(
                ["--initcmdreq_guid", PAIRING_GUID, "--initcmdreq_hostname", "camera-transfer"]
            )

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            backend_environment = os.environ.copy()
            backend_environment["CAMERA_WIFI_TRANSFER_GUI"] = "1"
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
                    if parsed.filename is None:
                        self.events.put(("log", self._friendly_backend_line(parsed.log_text)))
                    if parsed.log_text.startswith("Found camera"):
                        self.events.put(("status", self.t("camera_found")))
            exit_code = self.backend_process.wait()
            self.backend_process = None
            success = exit_code == 0
            if not success:
                self.events.put(
                    (
                        "log",
                        self.t("transfer_incomplete"),
                    )
                )
            self.events.put(
                (
                    "done",
                    {"success": success, "downloaded": downloaded, "skipped": skipped},
                )
            )
        except OSError as exc:
            self.backend_process = None
            self.events.put(("log", self.t("backend_start_failed", error=exc)))
            self.events.put(("done", {"success": False, "downloaded": 0, "skipped": 0}))

    def _friendly_backend_line(self, line: str) -> str:
        replacements = (
            ("Searching for a Sony or Canon camera via SSDP", self.t("searching_network")),
            ("Found camera at IP address", self.t("camera_ip_found")),
            ("No camera found via SSDP Discovery.", self.t("camera_not_found")),
            ("files downloaded", self.t("files_downloaded")),
            ("previously-downloaded files skipped", self.t("files_skipped")),
        )
        result = line
        for source, target in replacements:
            result = result.replace(source, target)
        return result

    def stop_transfer(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            return
        if not messagebox.askyesno(
            self.t("stop_title"),
            self.t("stop_question"),
            parent=self,
        ):
            return
        if self.backend_process and self.backend_process.poll() is None:
            self.backend_process.terminate()
            self._append_log(self.t("stopped_network"))
        else:
            self.cancel_requested.set()
            self._append_log(self.t("stopping_direct"))

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._append_log(str(payload))
                elif event == "status":
                    self.status_text.set(str(payload))
                elif event == "overall":
                    current, total = payload
                    self.overall_current = int(current)
                    self.overall_total = int(total)
                    self.progress.stop()
                    percent = int(self.overall_current * 100 / self.overall_total) if self.overall_total else 0
                    self.progress.configure(mode="determinate", value=percent)
                elif event == "file_progress":
                    filename, percent, current, total = payload
                    self.current_file_percent = int(percent)
                    if total:
                        overall_percent = int(max(0, current - 1) * 100 / total)
                        self.detail_text.set(
                            self.t(
                                "progress_detail",
                                current=current,
                                total=total,
                                overall=overall_percent,
                                filename=filename,
                                file_percent=percent,
                            )
                        )
                    else:
                        self.detail_text.set(
                            self.t("downloading_file", filename=filename, percent=percent)
                        )
                elif event == "storage":
                    total_bytes, free_bytes, label = payload
                    free_percent = int(free_bytes * 100 / total_bytes) if total_bytes else 0
                    self.storage_text.set(
                        self.t(
                            "storage_status",
                            label=label or self.t("memory_card"),
                            total=self._format_size(total_bytes),
                            free=self._format_size(free_bytes),
                            percent=free_percent,
                        )
                    )
                elif event == "done":
                    self._finish_transfer(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, line: str) -> None:
        cleaned = clean_backend_text(line)
        if not cleaned:
            return
        self.log.configure(state="normal")
        self.log.insert(END, cleaned + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    @staticmethod
    def _format_size(value: int) -> str:
        size = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def _finish_transfer(self, result: Any) -> None:
        if isinstance(result, dict):
            success = bool(result.get("success"))
            downloaded = int(result.get("downloaded", 0))
            skipped = int(result.get("skipped", 0))
        else:
            success = bool(result)
            downloaded = 0
            skipped = 0
        self.progress.stop()
        self.progress.configure(mode="determinate", value=100 if success else 0)
        self.start_button.configure(state="normal")
        self.selected_button.configure(state="normal" if self.mode.get() == "ptpip" else "disabled")
        self.stop_button.configure(state="disabled")
        if success:
            self.status_text.set(self.t("transfer_complete"))
            self.detail_text.set(
                self.t("done_counts", downloaded=downloaded, skipped=skipped)
            )
            if self.notify_on_complete.get() and not self.demo_mode:
                messagebox.showinfo(
                    self.t("transfer_complete"),
                    self.t(
                        "completion_message",
                        downloaded=downloaded,
                        skipped=skipped,
                        folder=self.destination.get(),
                    ),
                    parent=self,
                )
        else:
            self.status_text.set(self.t("needs_attention"))
            self.detail_text.set(self.t("transfer_not_completed"))

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                self.t("copy_in_progress"),
                self.t("finish_or_stop"),
                parent=self,
            )
            return
        self.save_settings()
        self.destroy()


def self_test() -> int:
    ui_error = ""
    ui_created = False
    test_app: ImportApp | None = None
    try:
        test_app = ImportApp(visible=False)
        test_app.update_idletasks()
        ui_created = True
    except Exception as exc:  # noqa: BLE001 - report packaging/UI failures
        ui_error = f"{type(exc).__name__}: {exc}"
    finally:
        if test_app is not None:
            test_app.destroy()

    checks = {
        "version": APP_VERSION,
        "sony_soap_import": bool(SonySoapClient),
        "ui_created": ui_created,
        "ui_error": ui_error,
        "backend_found": bool(backend_command()),
        "native_pairing_packet": len(PAIRING_CDB) == 9 and len(build_pairing_payload()) == 8192,
        "default_mode": Settings.DEFAULTS["mode"],
        "default_destination": Settings.DEFAULTS["destination"],
        "english_available": Translator("en")("download_new") == "Download new files",
        "russian_available": Translator("ru")("download_new") == "Скачать новые",
    }
    print(json.dumps(checks, ensure_ascii=False))
    return 0 if all(
        (
            checks["sony_soap_import"],
            checks["ui_created"],
            checks["native_pairing_packet"],
            checks["default_mode"] == "ptpip",
            Path(str(checks["default_destination"])).name == "Camera Imports",
            checks["english_available"],
            checks["russian_available"],
        )
    ) else 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--pair-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--language", choices=("auto", "ru", "en"), default=None)
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.pair_helper:
        if args.result_file is None:
            return 2
        return pairing_child(args.result_file, args.language or "auto")
    app = ImportApp(language_override=args.language, demo_mode=args.demo)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
