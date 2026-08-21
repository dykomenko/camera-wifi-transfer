from __future__ import annotations

from pathlib import Path
import sys
import unittest


MODULE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODULE_DIRECTORY))

from backend_output import BackendOutputParser, clean_backend_text  # noqa: E402


class BackendOutputTests(unittest.TestCase):
    def test_backspaces_never_reach_the_gui(self) -> None:
        self.assertEqual(clean_backend_text("\b\b\b 27%\r"), "27%")

    def test_progress_updates_status_without_log_spam(self) -> None:
        parser = BackendOutputParser()
        first = parser.parse('Downloading "DSC01149.ARW":  0%')
        update = parser.parse("\b\b\b 42%")

        self.assertEqual(first.filename, "DSC01149.ARW")
        self.assertEqual(first.percent, 0)
        self.assertEqual(first.log_text, 'Downloading "DSC01149.ARW"')
        self.assertEqual(update.filename, "DSC01149.ARW")
        self.assertEqual(update.percent, 42)
        self.assertIsNone(update.log_text)

    def test_terminal_clear_sequences_are_ignored(self) -> None:
        parser = BackendOutputParser()
        self.assertIsNone(parser.parse("\r" + " " * 78 + "\r").log_text)

    def test_structured_progress_and_storage(self) -> None:
        parser = BackendOutputParser()
        total = parser.parse("CWFT\tTOTAL\t100")
        item = parser.parse("CWFT\tITEM\t1\t100\tDSC01152.ARW\t25296896")
        progress = parser.parse("CWFT\tPROGRESS\t42")
        done = parser.parse("CWFT\tITEM_DONE\t1\t100\tDSC01152.ARW\tdownloaded")
        storage = parser.parse("CWFT\tSTORAGE\t64000000000\t32000000000\tSDXC")

        self.assertEqual(total.total, 100)
        self.assertEqual((item.current, item.total, item.filename), (1, 100, "DSC01152.ARW"))
        self.assertEqual(progress.percent, 42)
        self.assertTrue(done.item_done)
        self.assertFalse(done.item_skipped)
        self.assertEqual((storage.storage_total, storage.storage_free), (64000000000, 32000000000))

    def test_legacy_joined_progress_is_collapsed(self) -> None:
        parser = BackendOutputParser()
        parsed = parser.parse('Downloading "DSC01152.ARW": 0% 1% 2% 99% 100%')
        self.assertEqual(parsed.filename, "DSC01152.ARW")
        self.assertEqual(parsed.percent, 100)
        self.assertNotIn("0% 1%", parsed.log_text or "")


if __name__ == "__main__":
    unittest.main()
