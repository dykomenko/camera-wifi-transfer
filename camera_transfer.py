"""Camera Transfer: one window for Wi-Fi downloads and moving a Sony card.

The interface is HTML/CSS/JS in ``web/`` shown by pywebview through
Microsoft Edge WebView2; everything else is Python.  ``Api`` is what the
page calls (``window.pywebview.api``); the app pushes events back with
``window.app.onEvents([...])``.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import card_mover_core as card_core
from localization import LANGUAGE_NAMES, MESSAGES, Translator, resolve_language
from wifi_transfer import (
    Settings,
    TransferOptions,
    WifiTransfer,
    backend_command,
    pairing_child,
    resource_dir,
    run_elevated_pairing,
)
from windows_pairing import PairingError

APP_VERSION = "0.5.1-beta"
CARD_POLL_SECONDS = 2.0
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CameraTransfer"
OLD_RUN_VALUES = ("CameraCardMover",)
SETTING_KEYS = {
    "destination", "mode", "camera_ip", "include_jpeg", "include_raw", "include_video",
    "organize_by_date", "notify_on_complete", "auto_start",
}


# ----------------------------------------------------------------- helpers
def web_dir() -> Path:
    return resource_dir() / "web"


def format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def strings_for(language: str) -> dict[str, str]:
    merged = dict(MESSAGES["en"])
    merged.update(MESSAGES.get(language, {}))
    return merged


def launch_command(*arguments: str) -> str:
    if getattr(sys, "frozen", False):
        parts = [f'"{sys.executable}"']
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        parts = [f'"{interpreter}"', f'"{Path(__file__).resolve()}"']
    return " ".join([*parts, *arguments])


def card_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except OSError:
        return False


def set_card_autostart(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        for old in OLD_RUN_VALUES:
            try:
                winreg.DeleteValue(key, old)
            except OSError:
                pass
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, launch_command("--watch"))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


def wifi_summary(
    t: Translator, result: Any, total: int, notify: bool, folder: str, *, cancelled: bool = False
) -> dict[str, Any]:
    """Describe a finished Wi-Fi transfer: headline, card coverage, dialog text."""

    if isinstance(result, dict):
        success = bool(result.get("success"))
        downloaded = int(result.get("downloaded", 0))
        skipped = int(result.get("skipped", 0))
    else:
        success, downloaded, skipped = bool(result), 0, 0
    counts = t("done_counts", downloaded=downloaded, skipped=skipped)
    if cancelled:
        return {
            "success": False, "complete": False, "cancelled": True, "headline": t("ui_stopped"),
            "coverage": t("partial_counts", downloaded=downloaded) if downloaded else "",
            "counts": counts, "dialog": "",
        }
    if not success:
        coverage = t("transfer_not_completed")
        if downloaded:
            coverage += "\n" + t("partial_counts", downloaded=downloaded)
        return {
            "success": False, "complete": False, "headline": t("needs_attention"),
            "coverage": coverage, "counts": counts, "dialog": "",
        }
    # Before the card is wiped the user needs to know that every camera
    # file was either copied now or is already in the folder.
    complete = not (total and downloaded + skipped < total)
    if complete:
        coverage = t("coverage_complete", total=total) if total else ""
        headline = t("transfer_complete")
    else:
        coverage = t("coverage_incomplete", handled=downloaded + skipped, total=total)
        headline = t("needs_attention")
    dialog = ""
    if notify:
        dialog = t("completion_message", downloaded=downloaded, skipped=skipped, folder=folder)
        if coverage:
            dialog += "\n\n" + coverage
    return {
        "success": True, "complete": complete, "headline": headline,
        "coverage": coverage, "counts": counts, "dialog": dialog,
    }


# --------------------------------------------------------------------- app
class CameraTransferApp:
    def __init__(self, *, watch: bool = False, language: str | None = None) -> None:
        self.watch = watch
        self.settings = Settings()
        self.values = self.settings.load()
        if language:
            self.values["language"] = language
        self.values["destination"] = os.path.normpath(str(self.values["destination"]))
        self.language = resolve_language(str(self.values["language"]))
        self.t = Translator(self.language)
        self.wifi_events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.outbox: queue.Queue[list[Any]] = queue.Queue()
        self.wifi = WifiTransfer(self.t, self.wifi_events)
        self.wifi_total = 0
        # True once the camera answered and files are being handled; until
        # then a Wi-Fi run is only searching and may be stopped for a card.
        self.wifi_transferring = False
        self.window: Any = None
        self.ui_ready = threading.Event()
        self.closing = threading.Event()
        self.card_roots: list[str] = []
        self.card: card_core.Card | None = None
        self.card_state: dict[str, Any] = {"state": "none"}
        self.card_worker: threading.Thread | None = None
        self.card_cancel = threading.Event()
        self.lock = threading.Lock()
        # Decide the first tab before the window appears: a Sony card in the
        # reader wins over starting a Wi-Fi download.
        self._scan_cards(initial=True)
        self.initial_tab = "card" if self.card_state["state"] in ("ready", "empty", "error") else "wifi"

    # ---------------------------------------------------------- plumbing
    def push(self, kind: str, data: Any = None) -> None:
        self.outbox.put([kind, data])

    def _pump(self) -> None:
        while not self.closing.is_set():
            self.ui_ready.wait(0.5)
            batch: list[Any] = []
            try:
                while True:
                    batch.append(self._translate_wifi_event(*self.wifi_events.get_nowait()))
            except queue.Empty:
                pass
            try:
                while True:
                    batch.append(self.outbox.get_nowait())
            except queue.Empty:
                pass
            batch = [item for item in batch if item]
            if batch and self.ui_ready.is_set() and self.window is not None:
                try:
                    self.window.evaluate_js(f"window.app && window.app.onEvents({json.dumps(batch)})")
                except Exception:  # noqa: BLE001 - the window may be closing
                    pass
            time.sleep(0.1)

    def _translate_wifi_event(self, kind: str, payload: Any) -> list[Any] | None:
        if kind == "storage":
            total, free, label = payload
            percent = int(free * 100 / total) if total else 0
            text = self.t(
                "storage_status", label=label or self.t("memory_card"),
                total=format_size(total), free=format_size(free), percent=percent,
            )
            return ["storage", text]
        if kind in ("storage", "file_progress") and not self.wifi_transferring:
            self.wifi_transferring = True
            self.outbox.put(["wifi_transferring", True])
        if kind == "overall":
            current, total = payload
            self.wifi_total = int(total)
            if total and not self.wifi_transferring:
                self.wifi_transferring = True
                self.outbox.put(["wifi_transferring", True])
            if total:
                self._set_title(int(current * 100 / total))
            return ["overall", [int(current), int(total)]]
        if kind == "attention":
            self._bring_to_front()
            return None
        if kind == "done":
            self._set_title(None)
            self.wifi_transferring = False
            summary = wifi_summary(
                self.t, payload, self.wifi_total,
                bool(self.values.get("notify_on_complete")), str(self.values["destination"]),
                cancelled=self.wifi.cancel_requested.is_set(),
            )
            if summary["coverage"] and summary["success"]:
                self.wifi_events.put(("log", summary["coverage"]))
            if summary["dialog"]:
                self._bring_to_front()
            return ["wifi_done", summary]
        return [kind, payload]

    def _set_title(self, percent: int | None) -> None:
        if self.window is None:
            return
        base = self.t("ui_app_name")
        try:
            self.window.set_title(base if percent is None else f"{percent}% · {base}")
        except Exception:  # noqa: BLE001
            pass

    def _bring_to_front(self) -> None:
        if self.window is None:
            return
        try:
            self.window.show()
            self.window.restore()
            self.window.on_top = True
            self.window.on_top = False
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ cards
    def _card_moving(self) -> bool:
        return self.card_worker is not None and self.card_worker.is_alive()

    def _scan_cards(self, *, initial: bool = False) -> None:
        roots = card_core.find_sony_cards()
        if roots == self.card_roots:
            return
        self.card_roots = roots
        self.card = None
        if not roots:
            self.card_state = {"state": "none"}
            self.push("card_state", self.card_state)
            return
        if not initial:
            self.push("card_state", {"state": "scanning"})
        try:
            card = card_core.open_card(roots[0])
        except card_core.CardError as exc:
            self.card_state = {"state": "error", "message": self.describe_card_error(exc)}
        except OSError as exc:
            self.card_state = {"state": "error", "message": self.t("mover_failed", error=exc)}
        else:
            self.card = card
            photos = sum(f.kind == "photo" for f in card.files)
            self.card_state = {
                "state": "ready" if card.files else "empty",
                "photos": photos,
                "videos": len(card.files) - photos,
                "size": format_size(card.total_bytes),
            }
        self.push("card_state", self.card_state)
        if not initial and self.card_state["state"] == "ready":
            if self.wifi.running and not self.wifi_transferring:
                # Only searching for the camera: the card is what the user
                # wants now.  A download in progress is never interrupted.
                self.stop_wifi_search_for_card()
            if not self.wifi.running:
                self.push("switch_tab", "card")
            if self.watch:
                self._bring_to_front()

    def stop_wifi_search_for_card(self) -> bool:
        """Stop a Wi-Fi run that has not reached the camera; True if stopped."""

        if not self.wifi.running:
            return True
        if self.wifi_transferring:
            return False
        self.push("log", self.t("ui_wifi_search_stopped_for_card"))
        self.wifi.stop(quiet=True)
        worker = self.wifi.worker
        if worker is not None:
            worker.join(timeout=10)
        return not self.wifi.running

    def _card_monitor(self) -> None:
        while not self.closing.wait(CARD_POLL_SECONDS):
            if self._card_moving():
                continue
            try:
                self._scan_cards()
            except Exception:  # noqa: BLE001 - keep watching for cards
                pass

    def describe_card_error(self, exc: card_core.CardError) -> str:
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

    def _move_worker(self, card: card_core.Card, destination: str) -> None:
        last = {"percent": -1, "key": ""}

        def progress(key: str, done: int, total: int) -> None:
            percent = int(done * 100 / total) if total else 100
            if percent != last["percent"] or key != last["key"]:
                last.update(percent=percent, key=key)
                self.push("card_progress", {"key": key, "percent": percent})
                self._set_title(percent)

        try:
            result = card_core.move_all(
                card, destination, by_date=bool(self.values.get("organize_by_date")),
                progress=progress, cancel=self.card_cancel,
            )
        except card_core.CardError as exc:
            message = self.describe_card_error(exc)
            self.push("log", message)
            self.push("card_failed", {"message": message})
        except OSError as exc:
            message = self.t("mover_failed", error=exc)
            self.push("log", message)
            self.push("card_failed", {"message": message})
        else:
            count = len(result.copied) + len(result.already_there)
            for path in result.copied:
                self.push("log", self.t("saved_file", filename=path))
            self.push("log", self.t("mover_backup", folder=result.backup_folder))
            dialog = self.t("mover_done", count=count, folder=destination)
            self.push("card_done", {"count": count, "dialog": dialog, "summary": dialog.split("\n")[0]})
            self._bring_to_front()
        finally:
            self._set_title(None)
            self.card_roots = []  # rescan the card that was just changed

    # -------------------------------------------------------------- run
    def _on_shown(self) -> None:
        if windows_uses_dark_theme():
            try:
                use_dark_title_bar(int(str(self.window.native.Handle)))
            except Exception:  # noqa: BLE001 - cosmetic only
                pass

    def on_closing(self) -> bool:
        if self.wifi.running or self._card_moving():
            self.push("alert", {"title": self.t("copy_in_progress"), "message": self.t("finish_or_stop")})
            return False
        if self.watch and card_autostart_enabled():
            # Keep watching for the next card in the background.
            self.window.hide()
            return False
        self.closing.set()
        return True

    def run(self) -> int:
        import webview

        api = Api(self)
        self.window = webview.create_window(
            self.t("ui_app_name"),
            url=str(web_dir() / "index.html"),
            js_api=api,
            width=780,
            height=700,
            min_size=(640, 600),
            hidden=self.watch,
            background_color="#1b1d22" if windows_uses_dark_theme() else "#f3f4f6",
        )
        self.window.events.closing += self.on_closing
        self.window.events.shown += self._on_shown
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._card_monitor, daemon=True).start()
        try:
            webview.start(gui="edgechromium", private_mode=False, storage_path=str(data_dir() / "webview"))
        except Exception as exc:  # noqa: BLE001 - most likely WebView2 is missing
            show_native_error(self.t("ui_app_name"), self.t("ui_webview_missing") + f"\n\n{exc}")
            return 1
        finally:
            self.closing.set()
            if self.wifi.running:
                self.wifi.stop()
        return 0


def windows_uses_dark_theme() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def use_dark_title_bar(hwnd: int) -> None:
    """Match the window frame to the dark page (Windows 10 2004+ / 11)."""

    import ctypes

    value = ctypes.c_int(1)
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, older builds
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
        ) == 0:
            break


def data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CameraWiFiTransfer"


def show_native_error(title: str, text: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}", file=sys.stderr)


class Api:
    """Methods the page calls.  Attributes start with ``_`` so pywebview
    does not try to expose them to JavaScript."""

    def __init__(self, app: CameraTransferApp) -> None:
        self._app = app

    def init(self) -> dict[str, Any]:
        app = self._app
        return {
            "strings": strings_for(app.language),
            "language": app.language,
            "settings": app.values,
            "languages": [["auto", ""], *LANGUAGE_NAMES.items()],
            "version": f"{app.t('ui_app_name')} {APP_VERSION}",
            "tab": app.initial_tab,
            "card": app.card_state,
            "cardAutostart": card_autostart_enabled(),
        }

    def ready(self) -> None:
        app = self._app
        app.ui_ready.set()
        if app.initial_tab == "wifi" and not app.watch and app.values.get("auto_start"):
            # The app is normally opened right after the camera starts
            # "Send to Computer", so begin immediately.
            result = self.start_wifi(auto=True)
            if result["ok"]:
                app.push("wifi_started")
                app.push("log", app.t("auto_start_log"))

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        app = self._app
        if key in SETTING_KEYS:
            app.values[key] = value
            app.settings.save(app.values)
        return app.values

    def set_language(self, code: str) -> dict[str, Any]:
        app = self._app
        app.values["language"] = code
        app.settings.save(app.values)
        app.language = resolve_language(code)
        app.t = Translator(app.language)
        app.wifi.t = app.t
        if app.card_state.get("state") == "error" and app.card_roots:
            app.card_roots = []  # re-describe the error in the new language
        app._set_title(None)
        return {"strings": strings_for(app.language), "language": app.language, "settings": app.values}

    def choose_folder(self) -> str | None:
        import webview

        app = self._app
        start = str(app.values.get("destination") or Path.home())
        result = app.window.create_file_dialog(webview.FOLDER_DIALOG, directory=start)
        if result:
            return os.path.normpath(result[0] if isinstance(result, (list, tuple)) else result)
        return None

    def open_folder(self) -> None:
        path = Path(str(self._app.values["destination"])).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError:
            pass

    def start_wifi(self, auto: bool = False) -> dict[str, Any]:
        app = self._app
        t = app.t
        if app.wifi.running or app._card_moving():
            return {"ok": False, "title": t("copy_in_progress"), "message": t("finish_or_stop")}
        options = TransferOptions.from_settings(app.values)
        if options.mode == "ptpip" and not any(
            (options.include_jpeg, options.include_raw, options.include_video)
        ):
            return {"ok": False, "title": t("no_file_types"), "message": t("select_file_types")}
        if options.mode == "ptpip" and not backend_command():
            return {"ok": False, "title": t("needs_attention"), "message": t("backend_missing")}
        try:
            Path(options.destination).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "title": t("cannot_create_folder"), "message": str(exc)}
        app.wifi_total = 0
        app.wifi_transferring = False
        app.wifi.start(options, wait_for_camera=auto)
        return {"ok": True}

    def stop_wifi(self) -> None:
        if self._app.wifi.running:
            self._app.wifi.stop()

    def move_card(self) -> dict[str, Any]:
        app = self._app
        t = app.t
        card = app.card
        if app._card_moving():
            return {"ok": False, "message": t("finish_or_stop")}
        if app.wifi.running and not app.stop_wifi_search_for_card():
            return {"ok": False, "message": t("ui_card_busy_wifi")}
        if card is None or not card.files:
            return {"ok": False, "message": t("mover_no_card")}
        destination = os.path.normpath(os.path.expanduser(str(app.values["destination"])))
        try:
            os.makedirs(destination, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "message": f"{t('cannot_create_folder')}: {exc}"}
        app.card_cancel.clear()
        app.card_worker = threading.Thread(target=app._move_worker, args=(card, destination), daemon=True)
        app.card_worker.start()
        return {"ok": True}

    def stop_card(self) -> None:
        self._app.card_cancel.set()

    def pair_usb(self) -> dict[str, Any]:
        app = self._app
        result_path = Path(tempfile.gettempdir()) / f"camera-pair-{uuid.uuid4().hex}.json"
        try:
            exit_code = run_elevated_pairing(result_path, app.language)
            if not result_path.exists():
                raise PairingError(app.t("helper_no_result", code=exit_code))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            success = exit_code == 0 and bool(result.get("success"))
            message = str(result.get("message") or json.dumps(result, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - surface helper failures to the user
            success, message = False, str(exc)
        finally:
            for path in (result_path, result_path.with_suffix(".tmp")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if success:
            app.values["pairing_profile"] = "helper"
            app.settings.save(app.values)
            message += "\n\n" + app.t("pairing_success_suffix")
        else:
            message = app.t("pairing_failed_text", output=(message or app.t("no_output"))[-1800:])
        return {"success": success, "message": message}

    def set_card_autostart(self, enabled: bool) -> bool:
        try:
            set_card_autostart(bool(enabled))
        except OSError:
            pass
        return card_autostart_enabled()


def self_test() -> int:
    checks = {
        "version": APP_VERSION,
        "web_assets": all((web_dir() / name).is_file() for name in ("index.html", "app.css", "app.js")),
        "backend_found": bool(backend_command()),
        "languages": len(LANGUAGE_NAMES),
        "strings_complete": all(set(MESSAGES["en"]) <= set(m) for m in MESSAGES.values()),
    }
    try:
        import webview  # noqa: F401

        checks["webview_import"] = True
    except Exception:  # noqa: BLE001
        checks["webview_import"] = False
    print(json.dumps(checks, ensure_ascii=False))
    return 0 if all(checks[k] for k in ("web_assets", "strings_complete", "webview_import")) else 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--watch", action="store_true", help="stay hidden until a Sony card is inserted")
    parser.add_argument("--pair-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--language", default=None)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.pair_helper:
        if args.result_file is None:
            return 2
        return pairing_child(args.result_file, args.language or "auto")
    return CameraTransferApp(watch=args.watch, language=args.language).run()


if __name__ == "__main__":
    raise SystemExit(main())
