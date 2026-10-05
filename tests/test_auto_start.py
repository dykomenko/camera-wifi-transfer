import json
import os
import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402
from sony_soap import TransportError  # noqa: E402


class AutoStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.destination = root / "photos"
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)})
        env.start()
        self.addCleanup(env.stop)

    def _write_settings(self, **values: object) -> None:
        settings_dir = Path(self.temp.name) / "CameraWiFiTransfer"
        settings_dir.mkdir(parents=True, exist_ok=True)
        values.setdefault("destination", str(self.destination))
        (settings_dir / "settings.json").write_text(json.dumps(values), encoding="utf-8")

    def _launch(self) -> mock.MagicMock:
        try:
            with mock.patch.object(app.ImportApp, "start_transfer") as start:
                window = app.ImportApp(language_override="en")
                window.after(800, window.destroy)
                window.mainloop()
        except tk.TclError as exc:  # pragma: no cover - headless machine
            self.skipTest(f"Tk unavailable: {exc}")
        return start

    def test_transfer_starts_automatically_by_default(self) -> None:
        self._write_settings()
        start = self._launch()
        self.assertEqual(start.call_count, 1)
        start.assert_called_with(wait_for_camera=True)

    def test_auto_start_can_be_disabled(self) -> None:
        self._write_settings(auto_start=False)
        self.assertEqual(self._launch().call_count, 0)

    def test_demo_mode_never_starts_a_transfer(self) -> None:
        self._write_settings()
        with mock.patch.object(app.ImportApp, "start_transfer") as start:
            window = app.ImportApp(language_override="en", demo_mode=True)
            window.after(800, window.destroy)
            window.mainloop()
        start.assert_not_called()


class CompletionCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": temp.name})
        env.start()
        self.addCleanup(env.stop)
        try:
            self.window = app.ImportApp(visible=False, language_override="en")
        except tk.TclError as exc:  # pragma: no cover - headless machine
            self.skipTest(f"Tk unavailable: {exc}")
        self.addCleanup(self.window.destroy)
        self.window.notify_on_complete.set(False)

    def test_every_camera_file_accounted_for(self) -> None:
        self.window.overall_total = 5
        self.window._finish_transfer({"success": True, "downloaded": 3, "skipped": 2})
        self.assertEqual(self.window.status_text.get(), "Transfer complete")
        self.assertIn("All 5 files", self.window.log.get("1.0", "end"))

    def test_missing_files_are_flagged_before_the_card_is_erased(self) -> None:
        self.window.overall_total = 5
        self.window._finish_transfer({"success": True, "downloaded": 2, "skipped": 2})
        self.assertEqual(self.window.status_text.get(), "Needs attention")
        self.assertIn("only 4 of 5", self.window.log.get("1.0", "end"))


class CameraWaitTests(unittest.TestCase):
    """Auto-start keeps looking for a camera that is not ready yet."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.destination = Path(temp.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LOCALAPPDATA": temp.name}),
            mock.patch.object(app, "CAMERA_RETRY_PAUSE_SECONDS", 0.01),
            mock.patch.object(app, "backend_command", return_value=["backend"]),
            mock.patch.object(app, "primary_lan_ipv4", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.window = app.ImportApp(visible=False, language_override="en")
        except tk.TclError as exc:  # pragma: no cover - headless machine
            self.skipTest(f"Tk unavailable: {exc}")
        self.addCleanup(self.window.destroy)

    def _events(self) -> list[tuple[str, object]]:
        events = []
        while True:
            try:
                events.append(self.window.events.get_nowait())
            except queue.Empty:
                return events

    def _logs(self, events: list[tuple[str, object]]) -> list[str]:
        return [str(payload) for kind, payload in events if kind == "log"]

    def _run_network(self, results: list[tuple[int, int, int, int]], deadline: float | None):
        with mock.patch.object(self.window, "_run_backend_once", side_effect=results) as run:
            self.window._ptpip_worker(self.destination, deadline)
        events = self._events()
        return run, events, events[-1]

    def test_network_mode_retries_until_the_camera_appears(self) -> None:
        not_found = (app.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)
        run, events, done = self._run_network(
            [not_found, not_found, (0, 3, 2, 5)], time.monotonic() + 60
        )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(done, ("done", {"success": True, "downloaded": 3, "skipped": 2}))
        waits = [line for line in self._logs(events) if "keep looking" in line]
        self.assertEqual(len(waits), 1, "the wait is explained once, not on every retry")

    def test_manual_start_does_not_wait(self) -> None:
        run, _events, done = self._run_network(
            [(app.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], None
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_gives_up_when_the_wait_is_over(self) -> None:
        run, events, done = self._run_network(
            [(app.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], time.monotonic()
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])
        self.assertTrue(any("not found within" in line for line in self._logs(events)))

    def test_other_failures_are_not_retried(self) -> None:
        run, _events, done = self._run_network([(1, 0, 0, 0)], time.monotonic() + 60)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_stop_ends_the_wait(self) -> None:
        self.window.cancel_requested.set()
        run, _events, done = self._run_network(
            [(app.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], time.monotonic() + 60
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_direct_mode_retries_until_the_camera_appears(self) -> None:
        client = mock.Mock()
        client.probe.side_effect = [
            TransportError("unreachable"),
            TransportError("unreachable"),
            mock.Mock(friendly_name="ILCE-6500", model_name="ILCE-6500"),
        ]
        client.browse_all.return_value = []
        with mock.patch.object(app, "SonySoapClient", return_value=client):
            self.window._direct_worker(self.destination, time.monotonic() + 60)
        self.assertEqual(client.probe.call_count, 3)
        self.assertTrue(self._events()[-1][1]["success"])


if __name__ == "__main__":
    unittest.main()
