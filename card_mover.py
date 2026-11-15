"""Camera Card Mover: move everything off a Sony camera card, by a button.

Run normally, or with ``--watch`` (used by the "open when a card is
inserted" option) to stay hidden until a Sony card appears.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Any

import card_mover_core as core
from localization import Translator, resolve_language

MOVER_VERSION = "0.1.0-beta"
POLL_MS = 2000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CameraCardMover"


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CameraCardMover"


def default_destination() -> str:
    """Use the Wi-Fi app's download folder when it has one."""

    wifi = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CameraWiFiTransfer" / "settings.json"
    try:
        value = json.loads(wifi.read_text(encoding="utf-8")).get("destination")
        if value:
            return str(value)
    except (OSError, ValueError):
        pass
    return str(Path.home() / "Pictures" / "Camera Imports")


def wifi_language() -> str:
    wifi = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CameraWiFiTransfer" / "settings.json"
    try:
        return str(json.loads(wifi.read_text(encoding="utf-8")).get("language") or "auto")
    except (OSError, ValueError):
        return "auto"


class MoverSettings:
    def __init__(self) -> None:
        self.path = data_dir() / "settings.json"
        self.values: dict[str, Any] = {
            "destination": default_destination(),
            "organize_by_date": True,
            "language": wifi_language(),
        }
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.values.update({k: loaded[k] for k in self.values if k in loaded})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --watch'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).resolve()}" --watch'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


def format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class MoverApp(tk.Tk):
    def __init__(self, *, watch: bool = False, language: str | None = None, visible: bool = True) -> None:
        super().__init__()
        self.watch = watch
        self.settings = MoverSettings()
        self.t = Translator(resolve_language(language or str(self.settings.values["language"])))
        self.title(f"{self.t('mover_title')}  {MOVER_VERSION}")
        self.geometry("760x560")
        self.minsize(680, 500)
        self.destination = tk.StringVar(value=os.path.normpath(str(self.settings.values["destination"])))
        self.by_date = tk.BooleanVar(value=bool(self.settings.values["organize_by_date"]))
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self.status = tk.StringVar(value=self.t("mover_searching"))
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.card: core.Card | None = None
        self.card_roots: list[str] = []
        self.scanning = False
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if watch or not visible:
            self.withdraw()
        self._timers: dict[str, str] = {}
        self._timers["drain"] = self.after(100, self._drain)
        self._timers["poll"] = self.after(200, self._poll)

    # ---------------------------------------------------------------- ui
    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name, root=self).configure(family="Segoe UI", size=10)
        card = "#f6f8fb"
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground="#152238")
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 11), foreground="#152238")
        style.configure("Muted.TLabel", foreground="#5d6878")
        style.configure("Card.TFrame", background=card)
        style.configure("Status.TLabel", font=("Segoe UI Semibold", 11), foreground="#1f4f7a", background=card)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 9))
        style.configure("Secondary.TButton", padding=(13, 9))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(24, 20))
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text=self.t("mover_title"), style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text=self.t("mover_subtitle"), style="Muted.TLabel", wraplength=700, justify=LEFT).pack(
            anchor="w", pady=(3, 16)
        )

        status_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        status_card.pack(fill=X, pady=(0, 16))
        ttk.Label(
            status_card, textvariable=self.status, style="Status.TLabel", wraplength=690, justify=LEFT
        ).pack(anchor="w")
        self.progress = ttk.Progressbar(status_card, mode="determinate", maximum=100)
        self.progress.pack(fill=X, pady=(10, 0))

        ttk.Label(outer, text=self.t("destination"), style="Section.TLabel").pack(anchor="w")
        row = ttk.Frame(outer)
        row.pack(fill=X, pady=(7, 10))
        ttk.Entry(row, textvariable=self.destination).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(row, text=self.t("choose"), command=self._choose).pack(side=LEFT, padx=(8, 0))
        ttk.Button(row, text=self.t("open"), command=self._open_destination).pack(side=LEFT, padx=(6, 0))

        options = ttk.Frame(outer)
        options.pack(fill=X, pady=(0, 14))
        ttk.Checkbutton(options, text=self.t("date_folders"), variable=self.by_date, command=self._save).pack(
            side=LEFT
        )
        ttk.Checkbutton(
            options, text=self.t("mover_autostart"), variable=self.autostart, command=self._toggle_autostart
        ).pack(side=LEFT, padx=(22, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill=X, pady=(0, 12))
        self.move_button = ttk.Button(
            actions, text=self.t("mover_move_all"), style="Accent.TButton", command=self._start_move, state="disabled"
        )
        self.move_button.pack(side=LEFT)
        self.stop_button = ttk.Button(
            actions, text=self.t("stop"), style="Secondary.TButton", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side=LEFT, padx=(8, 0))

        log_frame = ttk.LabelFrame(outer, text=self.t("copy_progress"), padding=8)
        log_frame.pack(fill=BOTH, expand=True)
        self.log = tk.Text(
            log_frame, height=8, wrap="word", relief="flat", background="#f8f9fb",
            foreground="#273444", padx=8, pady=8, state="disabled", font=("Segoe UI", 9),
        )
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill="y")
        self.log.pack(fill=BOTH, expand=True)

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(END, line + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def _save(self) -> None:
        self.settings.values["destination"] = self.destination.get()
        self.settings.values["organize_by_date"] = self.by_date.get()
        self.settings.save()

    def _choose(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.destination.get(), parent=self)
        if selected:
            self.destination.set(os.path.normpath(selected))
            self._save()

    def _open_destination(self) -> None:
        path = Path(self.destination.get()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(self.t("error"), str(exc), parent=self)

    def _toggle_autostart(self) -> None:
        try:
            set_autostart(self.autostart.get())
        except OSError as exc:
            self.autostart.set(autostart_enabled())
            messagebox.showerror(self.t("error"), str(exc), parent=self)

    # ----------------------------------------------------------- polling
    def _poll(self) -> None:
        busy = self.worker is not None and self.worker.is_alive()
        if not busy and not self.scanning:
            roots = core.find_sony_cards()
            if roots != self.card_roots:
                self.card_roots = roots
                self.card = None
                self.move_button.configure(state="disabled")
                if roots:
                    self.scanning = True
                    self.status.set(self.t("mover_searching"))
                    threading.Thread(target=self._scan, args=(roots[0],), daemon=True).start()
                else:
                    self.status.set(self.t("mover_no_card"))
        self._timers["poll"] = self.after(POLL_MS, self._poll)

    def _scan(self, root: str) -> None:
        try:
            self.events.put(("card", core.open_card(root)))
        except core.CardError as exc:
            self.events.put(("card_error", exc))
        except OSError as exc:
            self.events.put(("card_error", core.CardError("io", str(exc))))

    def _describe_error(self, exc: core.CardError) -> str:
        key = exc.args[0] if exc.args else ""
        details = exc.args[1] if len(exc.args) > 1 else ""
        if key == "database_not_understood":
            return self.t("mover_database_error", details=details)
        if key == "card_and_database_differ":
            return self.t("mover_mismatch", details=details)
        if key == "cancelled":
            return self.t("mover_stopped")
        if key == "card_changed":
            return self.t("mover_card_changed")
        if key == "copy_mismatch":
            return self.t("mover_copy_mismatch", details=details)
        return self.t("mover_failed", error=" ".join(str(a) for a in exc.args))

    def _show_card(self, card: core.Card) -> None:
        self.card = card
        photos = sum(f.kind == "photo" for f in card.files)
        videos = len(card.files) - photos
        if not card.files:
            self.status.set(self.t("mover_card_empty"))
            self.move_button.configure(state="disabled")
            return
        self.status.set(
            self.t("mover_card_found", photos=photos, videos=videos, size=format_size(card.total_bytes))
        )
        self.move_button.configure(state="normal")
        if self.watch and self.state() == "withdrawn":
            self.deiconify()
            self.lift()
            self.bell()

    # -------------------------------------------------------------- move
    def _start_move(self) -> None:
        card = self.card
        if card is None or not card.files:
            return
        destination = os.path.normpath(os.path.expanduser(self.destination.get()))
        photos = sum(f.kind == "photo" for f in card.files)
        if not messagebox.askyesno(
            self.t("mover_confirm_title"),
            self.t(
                "mover_confirm", photos=photos, videos=len(card.files) - photos,
                size=format_size(card.total_bytes), folder=destination,
            ),
            parent=self,
        ):
            return
        try:
            os.makedirs(destination, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(self.t("cannot_create_folder"), str(exc), parent=self)
            return
        self._save()
        self.cancel.clear()
        self.move_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append(self.t("new_run"))
        self.worker = threading.Thread(target=self._move, args=(card, destination), daemon=True)
        self.worker.start()

    def _move(self, card: core.Card, destination: str) -> None:
        def progress(key: str, done: int, total: int) -> None:
            self.events.put(("progress", (key, done, total)))

        try:
            result = core.move_all(
                card, destination, by_date=self.by_date.get(), progress=progress, cancel=self.cancel
            )
            self.events.put(("done", (result, destination)))
        except core.CardError as exc:
            self.events.put(("failed", exc))
        except OSError as exc:
            self.events.put(("failed", core.CardError("io", str(exc))))

    def _stop(self) -> None:
        self.cancel.set()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "card":
                    self.scanning = False
                    self._show_card(payload)
                elif kind == "card_error":
                    self.scanning = False
                    self.card = None
                    self.status.set(self._describe_error(payload))
                elif kind == "progress":
                    key, done, total = payload
                    percent = int(done * 100 / total) if total else 100
                    self.progress.configure(value=percent)
                    if key == "copying":
                        self.status.set(self.t("mover_copying", percent=percent))
                    else:
                        self.status.set(self.t("mover_updating"))
                elif kind == "done":
                    result, destination = payload
                    self._finish()
                    count = len(result.copied) + len(result.already_there)
                    for path in result.copied:
                        self._append(self.t("saved_file", filename=path))
                    self._append(self.t("mover_backup", folder=result.backup_folder))
                    self.status.set(self.t("mover_card_empty"))
                    self.progress.configure(value=100)
                    self.card_roots = []  # rescan the now empty card
                    if messagebox.askyesno(
                        self.t("mover_done_title"),
                        self.t("mover_done", count=count, folder=destination)
                        + "\n\n" + self.t("open_folder_question"),
                        parent=self,
                    ):
                        self._open_destination()
                elif kind == "failed":
                    self._finish()
                    message = self._describe_error(payload)
                    self.status.set(message)
                    self._append(message)
                    self.progress.configure(value=0)
                    self.card_roots = []
                    messagebox.showerror(self.t("needs_attention"), message, parent=self)
        except queue.Empty:
            pass
        self._timers["drain"] = self.after(100, self._drain)

    def destroy(self) -> None:
        for timer in self._timers.values():
            try:
                self.after_cancel(timer)
            except tk.TclError:
                pass
        self._timers.clear()
        super().destroy()

    def _finish(self) -> None:
        self.stop_button.configure(state="disabled")
        self.worker = None

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showwarning(self.t("copy_in_progress"), self.t("finish_or_stop"), parent=self)
            return
        self._save()
        if self.watch and self.autostart.get():
            # Keep watching for the next card in the background.
            self.withdraw()
            self.card_roots = []
            return
        self.destroy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="stay hidden until a Sony card is inserted")
    parser.add_argument("--language", default=None)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        app = MoverApp(visible=False)
        app.update_idletasks()
        app.destroy()
        print(json.dumps({"version": MOVER_VERSION, "ui_created": True}))
        return 0
    app = MoverApp(watch=args.watch, language=args.language)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
