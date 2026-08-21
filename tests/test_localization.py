from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


MODULE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODULE_DIRECTORY))

import localization  # noqa: E402


class LocalizationTests(unittest.TestCase):
    def test_explicit_english_and_russian(self) -> None:
        self.assertEqual(localization.Translator("en")("download_new"), "Download new files")
        self.assertEqual(localization.Translator("ru")("download_new"), "Скачать новые")

    def test_auto_uses_windows_ui_language(self) -> None:
        with mock.patch.object(localization, "detect_system_language", return_value="en"):
            self.assertEqual(localization.resolve_language("auto"), "en")

    def test_unknown_key_falls_back_without_crashing(self) -> None:
        self.assertEqual(localization.Translator("en")("missing_key"), "missing_key")


if __name__ == "__main__":
    unittest.main()
