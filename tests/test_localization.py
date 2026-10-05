from __future__ import annotations

from pathlib import Path
import string
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

    def test_every_language_matches_english_keys_and_placeholders(self) -> None:
        english = localization.MESSAGES["en"]
        for code in localization.SUPPORTED_LANGUAGES:
            messages = localization.MESSAGES[code]
            with self.subTest(language=code):
                self.assertEqual(set(messages), set(english))
                for key, template in english.items():
                    self.assertEqual(
                        _placeholders(messages[key]), _placeholders(template), f"{code}:{key}"
                    )

    def test_every_language_has_a_native_name(self) -> None:
        self.assertEqual(set(localization.LANGUAGE_NAMES), set(localization.MESSAGES))
        self.assertEqual(localization.LANGUAGE_NAMES["be"], "Беларуская")

    def test_windows_ui_language_selects_belarusian(self) -> None:
        windll = mock.Mock()
        windll.kernel32.GetUserDefaultUILanguage.return_value = 0x0423  # be-BY
        with mock.patch.object(localization.os, "name", "nt"), mock.patch.object(
            localization.ctypes, "windll", windll, create=True
        ):
            self.assertEqual(localization.detect_system_language(), "be")

    def test_unsupported_windows_language_falls_back_to_english(self) -> None:
        windll = mock.Mock()
        windll.kernel32.GetUserDefaultUILanguage.return_value = 0x0425  # et-EE
        with mock.patch.object(localization.os, "name", "nt"), mock.patch.object(
            localization.ctypes, "windll", windll, create=True
        ):
            self.assertEqual(localization.detect_system_language(), "en")

    def test_locale_fallback_matches_whole_language_names(self) -> None:
        with mock.patch.object(localization.os, "name", "posix"):
            for reported, expected in (
                ("be_BY", "be"),
                ("Belarusian_Belarus", "be"),
                ("Estonian_Estonia", "en"),
            ):
                with mock.patch.object(
                    localization.locale, "getlocale", return_value=(reported, "UTF-8")
                ):
                    self.assertEqual(localization.detect_system_language(), expected)


def _placeholders(template: str) -> list[str]:
    return sorted(name for _, name, _, _ in string.Formatter().parse(template) if name)


if __name__ == "__main__":
    unittest.main()
