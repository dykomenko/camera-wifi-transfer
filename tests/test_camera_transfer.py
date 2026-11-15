import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import camera_transfer as ct  # noqa: E402
import card_mover_core  # noqa: E402
from localization import MESSAGES, Translator  # noqa: E402


class SummaryTests(unittest.TestCase):
    t = Translator("en")

    def test_every_camera_file_accounted_for(self) -> None:
        summary = ct.wifi_summary(self.t, {"success": True, "downloaded": 3, "skipped": 2}, 5, True, "C:\\x")
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["headline"], "Transfer complete")
        self.assertIn("All 5 files", summary["coverage"])
        self.assertIn("All 5 files", summary["dialog"])

    def test_missing_files_are_flagged_before_the_card_is_erased(self) -> None:
        summary = ct.wifi_summary(self.t, {"success": True, "downloaded": 2, "skipped": 2}, 5, False, "C:\\x")
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["headline"], "Needs attention")
        self.assertIn("only 4 of 5", summary["coverage"])
        self.assertEqual(summary["dialog"], "", "no dialog when notifications are off")

    def test_failed_run_reports_files_saved_before_the_drop(self) -> None:
        summary = ct.wifi_summary(self.t, {"success": False, "downloaded": 3, "skipped": 0}, 10, True, "C:\\x")
        self.assertFalse(summary["success"])
        self.assertIn("before the connection broke: 3", summary["coverage"])

    def test_backend_missing_result_is_a_failure(self) -> None:
        self.assertFalse(ct.wifi_summary(self.t, False, 0, True, "C:\\x")["success"])


class StartupTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = temp.name
        env = mock.patch.dict(os.environ, {"LOCALAPPDATA": temp.name})
        env.start()
        self.addCleanup(env.stop)

    def _app(self, cards: list[str], card: object = None, **kwargs: object) -> ct.CameraTransferApp:
        with mock.patch.object(card_mover_core, "find_sony_cards", return_value=cards), mock.patch.object(
            card_mover_core, "open_card", return_value=card
        ):
            app = ct.CameraTransferApp(language="en", **kwargs)
        # Never create folders in the real Pictures folder.
        app.values["destination"] = os.path.join(self.temp, "photos")
        return app

    def test_wifi_download_starts_automatically_without_a_card(self) -> None:
        app = self._app([])
        self.assertEqual(app.initial_tab, "wifi")
        with mock.patch.object(app.wifi, "start") as start:
            ct.Api(app).ready()
        start.assert_called_once()
        self.assertTrue(start.call_args.kwargs["wait_for_camera"])

    def test_inserted_card_opens_the_card_tab_and_skips_wifi(self) -> None:
        card = mock.Mock(files=[mock.Mock(kind="photo"), mock.Mock(kind="video")], total_bytes=2048)
        app = self._app(["G:\\"], card)
        self.assertEqual(app.initial_tab, "card")
        self.assertEqual(app.card_state["state"], "ready")
        self.assertEqual((app.card_state["photos"], app.card_state["videos"]), (1, 1))
        with mock.patch.object(app.wifi, "start") as start:
            ct.Api(app).ready()
        start.assert_not_called()

    def test_auto_start_can_be_disabled(self) -> None:
        app = self._app([])
        app.values["auto_start"] = False
        with mock.patch.object(app.wifi, "start") as start:
            ct.Api(app).ready()
        start.assert_not_called()

    def test_watch_mode_never_starts_a_wifi_download(self) -> None:
        app = self._app([], watch=True)
        with mock.patch.object(app.wifi, "start") as start:
            ct.Api(app).ready()
        start.assert_not_called()

    def test_card_with_an_unknown_database_is_explained(self) -> None:
        with mock.patch.object(card_mover_core, "find_sony_cards", return_value=["G:\\"]), mock.patch.object(
            card_mover_core, "open_card", side_effect=card_mover_core.CardError("card_and_database_differ", "+X")
        ):
            app = ct.CameraTransferApp(language="en")
        self.assertEqual(app.card_state["state"], "error")
        self.assertIn("Recover Image DB", app.card_state["message"])

    def test_no_file_types_is_refused(self) -> None:
        app = self._app([])
        app.values.update(include_jpeg=False, include_raw=False, include_video=False, mode="ptpip")
        result = ct.Api(app).start_wifi()
        self.assertFalse(result["ok"])
        self.assertEqual(result["title"], "No file types selected")


class CardWhileWifiTests(StartupTests):
    def _ready_card(self) -> object:
        return mock.Mock(files=[mock.Mock(kind="photo")], total_bytes=1024)

    def _insert_card(self, app: ct.CameraTransferApp) -> None:
        with mock.patch.object(card_mover_core, "find_sony_cards", return_value=["G:\\"]), mock.patch.object(
            card_mover_core, "open_card", return_value=self._ready_card()
        ):
            app._scan_cards()

    def _events(self, app: ct.CameraTransferApp) -> list:
        events = []
        while not app.outbox.empty():
            events.append(app.outbox.get_nowait())
        return events

    def test_inserting_a_card_stops_a_camera_search(self) -> None:
        app = self._app([])
        with mock.patch.object(type(app.wifi), "running", new_callable=mock.PropertyMock) as running,                 mock.patch.object(app.wifi, "stop") as stop:
            running.side_effect = [True, True, False, False]
            self._insert_card(app)
        stop.assert_called_once_with(quiet=True)
        self.assertIn(["switch_tab", "card"], self._events(app))

    def test_a_running_download_is_not_interrupted_by_a_card(self) -> None:
        app = self._app([])
        app.wifi_transferring = True
        with mock.patch.object(type(app.wifi), "running", new_callable=mock.PropertyMock, return_value=True),                 mock.patch.object(app.wifi, "stop") as stop:
            self._insert_card(app)
            result = ct.Api(app).move_card()
        stop.assert_not_called()
        self.assertNotIn(["switch_tab", "card"], self._events(app))
        self.assertFalse(result["ok"])
        self.assertIn("Wi-Fi download", result["message"])

    def test_stopped_run_is_reported_as_stopped_not_failed(self) -> None:
        summary = ct.wifi_summary(Translator("en"), {"success": False, "downloaded": 0, "skipped": 0}, 0, True,
                                  "C:/x", cancelled=True)
        self.assertEqual(summary["headline"], "Stopped")
        self.assertTrue(summary["cancelled"])


class InterfaceTextTests(unittest.TestCase):
    def test_every_text_the_window_uses_exists_in_every_language(self) -> None:
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        keys = set(re.findall(r'data-t="([a-z_]+)"', html)) | set(re.findall(r'\bt\("([a-z_]+)"', script))
        self.assertGreater(len(keys), 40)
        for language, messages in MESSAGES.items():
            with self.subTest(language=language):
                self.assertEqual(sorted(keys - set(messages)), [])

    def test_self_test_passes(self) -> None:
        with mock.patch("builtins.print"):
            self.assertEqual(ct.self_test(), 0)


if __name__ == "__main__":
    unittest.main()
