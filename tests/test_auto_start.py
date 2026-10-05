import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


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
        self.assertEqual(self._launch().call_count, 1)

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


if __name__ == "__main__":
    unittest.main()
