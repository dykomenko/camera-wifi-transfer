from __future__ import annotations

import ctypes
import locale
import os
from typing import Any

from locales import be, de, en, es, fr, it, ja, pl, pt, ru, uk, zh


# Native language names for the selector, in display order.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
    "be": "Беларуская",
    "uk": "Українська",
    "pl": "Polski",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pt": "Português",
    "zh": "简体中文",
    "ja": "日本語",
}
SUPPORTED_LANGUAGES = tuple(LANGUAGE_NAMES)

MESSAGES: dict[str, dict[str, str]] = {
    module.__name__.rsplit(".", 1)[-1]: module.MESSAGES
    for module in (en, ru, be, uk, pl, de, fr, es, it, pt, zh, ja)
}

# Windows primary language IDs (LANGID & 0x3FF).
WINDOWS_PRIMARY_LANGUAGES: dict[int, str] = {
    0x09: "en",
    0x19: "ru",
    0x23: "be",
    0x22: "uk",
    0x15: "pl",
    0x07: "de",
    0x0C: "fr",
    0x0A: "es",
    0x10: "it",
    0x16: "pt",
    0x04: "zh",
    0x11: "ja",
}
LOCALE_LANGUAGE_NAMES: dict[str, str] = {
    "english": "en",
    "russian": "ru",
    "belarusian": "be",
    "ukrainian": "uk",
    "polish": "pl",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "chinese": "zh",
    "japanese": "ja",
}


def detect_system_language() -> str:
    if os.name == "nt":
        try:
            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            return WINDOWS_PRIMARY_LANGUAGES.get(language_id & 0x3FF, "en")
        except (AttributeError, OSError, ValueError):
            pass
    # POSIX reports "be_BY"; Windows may report "Belarusian_Belarus".
    language = (locale.getlocale()[0] or "").lower().replace("-", "_").split("_")[0]
    if language in SUPPORTED_LANGUAGES:
        return language
    return LOCALE_LANGUAGE_NAMES.get(language, "en")


def resolve_language(preference: str | None) -> str:
    if preference in SUPPORTED_LANGUAGES:
        return str(preference)
    return detect_system_language()


class Translator:
    def __init__(self, language: str) -> None:
        self.language = resolve_language(language)

    def __call__(self, key: str, **values: Any) -> str:
        template = MESSAGES.get(self.language, MESSAGES["en"]).get(key)
        if template is None:
            template = MESSAGES["en"].get(key, key)
        return template.format(**values)
