from __future__ import annotations

import ctypes
import locale
import os
from typing import Any


SUPPORTED_LANGUAGES = ("ru", "en")


MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "app_title": "Camera Wi-Fi Transfer",
        "app_subtitle": "Copies photos and videos over Wi-Fi. Files on the camera are never changed.",
        "language": "Language",
        "language_auto": "Auto",
        "language_ru": "Русский",
        "language_en": "English",
        "transfer_mode": "Transfer mode",
        "direct_mode": "Direct camera connection — JPEG (Sony)",
        "network_mode": "Home network — original JPEG/RAW/video",
        "direct_instruction": (
            "Sony cameras: open Send to Smartphone → Select on Smartphone, then connect Windows "
            "to the camera's DIRECT-… Wi-Fi network. RAW files are delivered as JPEG in this mode."
        ),
        "network_instruction": (
            "Connect the camera and PC to the same network, then enable Send to Computer / PC transfer "
            "on the camera. Some older Sony cameras require one-time USB pairing."
        ),
        "destination": "Download folder",
        "choose": "Browse…",
        "open": "Open",
        "jpeg_heif": "JPEG / HEIF",
        "raw": "RAW",
        "video": "Video",
        "date_folders": "Folders by date",
        "camera_ip": "Camera IP (optional)",
        "camera_ip_hint": "Automatic discovery when empty",
        "download_new": "Download new files",
        "download_selected": "Download selected",
        "selected_title": "Download selected files",
        "selected_instructions": (
            "First select the photos in the camera's Send to Computer / PC transfer list, then start that mode. "
            "This button downloads only the camera's selected transfer list. Continue?"
        ),
        "stop": "Stop",
        "sony_pairing": "Sony USB pairing…",
        "copy_progress": " Transfer log ",
        "ready": "Ready",
        "waiting_transfer": "Choose what to download",
        "preparing_transfer": "Reading the camera and preparing the transfer…",
        "notify_complete": "Notify when done",
        "transfer_complete": "Transfer complete",
        "transfer_not_completed": "The transfer did not complete. See the log below.",
        "completion_message": (
            "All copying operations have finished.\n\nDownloaded: {downloaded}\nAlready present: {skipped}\n\nFolder: {folder}"
        ),
        "progress_detail": "{current} of {total} · total {overall}% · {filename} {file_percent}%",
        "storage_unknown": "Card space will appear after connection",
        "storage_reading": "Reading card space…",
        "storage_status": "{label}: {free} free of {total} ({percent}%)",
        "memory_card": "Memory card",
        "needs_attention": "Needs attention",
        "connecting": "Connecting…",
        "new_run": "— New transfer —",
        "demo_log": "Ready. Choose a transfer mode, select a folder, and click Download new files.",
        "error": "Error",
        "cannot_create_folder": "Cannot create folder",
        "no_file_types": "No file types selected",
        "select_file_types": "Select JPEG/HEIF, RAW and/or video.",
        "looking_direct": "Looking for a camera at 192.168.122.1…",
        "camera_found": "Camera found",
        "camera_found_reading": "Camera found: {camera}. Reading the file list…",
        "files_available": "Files available: {count}",
        "copy_stopped": "Transfer stopped by the user.",
        "downloading_count": "Downloading {current} of {total}",
        "checking_file": "Checking: {filename}",
        "saved_file": "Saved: {filename}",
        "already_exists": "Already present: {filename}",
        "done_counts": "Done. Downloaded: {downloaded}; already present: {skipped}.",
        "camera_error": "Camera error: {error}",
        "failed": "Failed: {error}",
        "session_close_failed": "Could not close the camera session: {error}",
        "backend_missing": "PTP/IP backend was not found next to the application.",
        "pc_network": "PC network interface: {address}",
        "network_auto_failed": "Could not determine the main network interface; using automatic discovery.",
        "transfer_incomplete": (
            "Transfer did not finish. Press Cancel on the camera, reopen Send to Computer / PC transfer, "
            "and try again."
        ),
        "backend_start_failed": "Could not start the PTP/IP backend: {error}",
        "searching_network": "Looking for a compatible camera on the local network…",
        "camera_ip_found": "Camera found, IP:",
        "camera_not_found": "Camera not found.",
        "files_downloaded": "files downloaded",
        "files_skipped": "previously downloaded files skipped",
        "downloading_file": "Downloading {filename}: {percent}%",
        "downloading_file_no_percent": "Downloading: {filename}",
        "stop_title": "Stop transfer?",
        "stop_question": "An unfinished .part file will not be treated as complete. Stop now?",
        "stopped_network": "Transfer stopped. Press Cancel on the camera.",
        "stopping_direct": "Direct JPEG transfer will stop after the current network operation.",
        "copy_in_progress": "Transfer in progress",
        "finish_or_stop": "Finish or stop the transfer first.",
        "pairing_title": "Sony USB pairing",
        "pairing_heading": "For compatible Sony cameras",
        "pairing_intro": (
            "One-time pairing is needed only by some Sony cameras for original JPEG/RAW/video transfer. "
            "A phone or Wi-Fi adapter in the PC is not required."
        ),
        "pairing_steps": (
            "1. Charge the battery and insert a memory card.\n\n"
            "2. On the camera, set USB Connection to Mass Storage.\n\n"
            "3. Connect the camera directly to the PC with a data-capable USB cable.\n\n"
            "4. Click the button below. The app writes only the authorized-client GUID; it does not touch photos."
        ),
        "pairing_warning": (
            "Windows will show one User Account Control prompt. The app uses the standard USB Mass Storage "
            "driver, verifies the SONY PMHOME service volume, and does not replace drivers or modify the card."
        ),
        "pairing_ready": "Ready to pair",
        "pair_camera": "Pair Sony camera by USB",
        "close": "Close",
        "confirm_pairing": "Confirm pairing",
        "pairing_question": (
            "Is the camera charged, set to USB Mass Storage, and connected directly?\n\n"
            "Only the SONY PMHOME service volume will be checked, then this PC's GUID will be registered. Continue?"
        ),
        "waiting_uac": "Waiting for Windows UAC confirmation…",
        "uac_cancelled": "Windows UAC confirmation was cancelled",
        "pair_timeout": "USB pairing did not finish within 60 seconds",
        "helper_no_result": "The USB helper exited with code {code} but did not produce a result",
        "pairing_written": "Pairing saved. You can disconnect USB.",
        "pairing_success_title": "Done",
        "pairing_success_suffix": "Disconnect USB and enable Send to Computer / PC transfer on the camera.",
        "pairing_not_confirmed": "Pairing was not confirmed",
        "no_output": "No diagnostic output",
        "pairing_failed_title": "Pairing failed",
        "pairing_failed_text": "The GUID write could not be confirmed. Save this diagnostic output:\n\n{output}",
        "pairing_success_message": (
            "Verified {vendor} {product} on {drive}. The GUID was written through the standard Windows driver."
        ),
    },
    "ru": {
        "app_title": "Camera Wi-Fi Transfer",
        "app_subtitle": "Копирует фото и видео по Wi-Fi. Файлы на камере не изменяются.",
        "language": "Язык",
        "language_auto": "Авто",
        "language_ru": "Русский",
        "language_en": "English",
        "transfer_mode": "Режим передачи",
        "direct_mode": "Прямо к камере — JPEG (Sony)",
        "network_mode": "Через домашнюю сеть — оригиналы JPEG/RAW/видео",
        "direct_instruction": (
            "Для Sony: откройте «Отправить на смартфон → Выбрать на смартфоне», затем подключите Windows "
            "к сети DIRECT-… камеры. RAW в этом режиме передаётся как JPEG."
        ),
        "network_instruction": (
            "Подключите камеру и ПК к одной сети, затем включите на камере «Отправить на компьютер» / "
            "PC transfer. Некоторым старым Sony нужна однократная USB-привязка."
        ),
        "destination": "Папка для скачивания",
        "choose": "Выбрать…",
        "open": "Открыть",
        "jpeg_heif": "JPEG / HEIF",
        "raw": "RAW",
        "video": "Видео",
        "date_folders": "Папки по датам",
        "camera_ip": "IP камеры (необязательно)",
        "camera_ip_hint": "Пусто — автоматический поиск",
        "download_new": "Скачать новые",
        "download_selected": "Скачать выбранные",
        "selected_title": "Скачать выбранные файлы",
        "selected_instructions": (
            "Сначала отметьте нужные фото в списке передачи «Отправить на компьютер» / PC transfer на камере, "
            "затем запустите этот режим. Кнопка скачает только выбранный камерой список. Продолжить?"
        ),
        "stop": "Остановить",
        "sony_pairing": "USB-привязка Sony…",
        "copy_progress": " Ход копирования ",
        "ready": "Готово",
        "waiting_transfer": "Выберите, что скачать",
        "preparing_transfer": "Читаю камеру и готовлю передачу…",
        "notify_complete": "Сообщить о завершении",
        "transfer_complete": "Копирование завершено",
        "transfer_not_completed": "Копирование не завершено. Подробности — в журнале ниже.",
        "completion_message": (
            "Все операции копирования завершены.\n\nСкачано: {downloaded}\nУже было: {skipped}\n\nПапка: {folder}"
        ),
        "progress_detail": "{current} из {total} · всего {overall}% · {filename} {file_percent}%",
        "storage_unknown": "Место на карте появится после подключения",
        "storage_reading": "Читаю свободное место…",
        "storage_status": "{label}: свободно {free} из {total} ({percent}%)",
        "memory_card": "Карта памяти",
        "needs_attention": "Нужна проверка",
        "connecting": "Подключение…",
        "new_run": "— Новый запуск —",
        "demo_log": "Готово. Выберите режим, папку и нажмите «Скачать новые».",
        "error": "Ошибка",
        "cannot_create_folder": "Нельзя создать папку",
        "no_file_types": "Нет типов файлов",
        "select_file_types": "Отметьте JPEG/HEIF, RAW и/или видео.",
        "looking_direct": "Ищу камеру по адресу 192.168.122.1…",
        "camera_found": "Камера найдена",
        "camera_found_reading": "Камера найдена: {camera}. Читаю список файлов…",
        "files_available": "Доступно файлов: {count}",
        "copy_stopped": "Копирование остановлено пользователем.",
        "downloading_count": "Скачиваю {current} из {total}",
        "checking_file": "Проверяю: {filename}",
        "saved_file": "Сохранено: {filename}",
        "already_exists": "Уже есть: {filename}",
        "done_counts": "Готово. Скачано: {downloaded}; уже было: {skipped}.",
        "camera_error": "Ошибка камеры: {error}",
        "failed": "Не получилось: {error}",
        "session_close_failed": "Не удалось закрыть сеанс камеры: {error}",
        "backend_missing": "PTP/IP-backend не найден рядом с программой.",
        "pc_network": "Сетевой интерфейс ПК: {address}",
        "network_auto_failed": "Не удалось определить основную сеть; пробую автоматический поиск.",
        "transfer_incomplete": (
            "Передача не завершена. На камере нажмите Cancel, заново включите «Отправить на компьютер» / "
            "PC transfer и повторите."
        ),
        "backend_start_failed": "Не удалось запустить PTP/IP-backend: {error}",
        "searching_network": "Ищу совместимую камеру в домашней сети…",
        "camera_ip_found": "Камера найдена, IP:",
        "camera_not_found": "Камера не найдена.",
        "files_downloaded": "файлов скачано",
        "files_skipped": "ранее скачанных файлов пропущено",
        "downloading_file": "Скачиваю {filename}: {percent}%",
        "downloading_file_no_percent": "Скачиваю: {filename}",
        "stop_title": "Остановить?",
        "stop_question": "Незавершённый .part-файл не будет считаться готовым. Остановить сейчас?",
        "stopped_network": "Передача остановлена. На камере нажмите Cancel.",
        "stopping_direct": "Прямой JPEG-режим будет остановлен после текущего сетевого шага.",
        "copy_in_progress": "Идёт копирование",
        "finish_or_stop": "Сначала завершите или остановите копирование.",
        "pairing_title": "USB-привязка Sony",
        "pairing_heading": "Для совместимых камер Sony",
        "pairing_intro": (
            "Однократная привязка нужна только некоторым Sony для передачи оригинальных JPEG/RAW/видео. "
            "Телефон и Wi-Fi-модуль в ПК не требуются."
        ),
        "pairing_steps": (
            "1. Зарядите аккумулятор и вставьте карту памяти.\n\n"
            "2. На камере выберите USB-соединение → Mass Storage.\n\n"
            "3. Подключите камеру напрямую к ПК USB-кабелем с передачей данных.\n\n"
            "4. Нажмите кнопку ниже. Программа запишет только GUID разрешённого клиента; фото она не трогает."
        ),
        "pairing_warning": (
            "Windows один раз покажет запрос контроля учётных записей. Программа использует штатный USB "
            "Mass Storage-драйвер, проверяет служебный диск SONY PMHOME и не меняет драйверы или карту."
        ),
        "pairing_ready": "Готово к привязке",
        "pair_camera": "Привязать Sony по USB",
        "close": "Закрыть",
        "confirm_pairing": "Подтвердите привязку",
        "pairing_question": (
            "Камера заряжена, включён USB Mass Storage и кабель подключён напрямую?\n\n"
            "Будет проверен только служебный диск SONY PMHOME, затем записан GUID этого ПК. Продолжить?"
        ),
        "waiting_uac": "Ожидаю подтверждение Windows UAC…",
        "uac_cancelled": "Подтверждение Windows UAC отменено",
        "pair_timeout": "USB-привязка не завершилась за 60 секунд",
        "helper_no_result": "USB-helper завершился с кодом {code}, но не оставил результат",
        "pairing_written": "Привязка записана. Можно отсоединить USB.",
        "pairing_success_title": "Готово",
        "pairing_success_suffix": "Отсоедините USB и включите на камере «Отправить на компьютер» / PC transfer.",
        "pairing_not_confirmed": "Привязка не подтверждена",
        "no_output": "Нет диагностического вывода",
        "pairing_failed_title": "Не получилось",
        "pairing_failed_text": "Не удалось подтвердить запись GUID. Сохраните этот вывод для диагностики:\n\n{output}",
        "pairing_success_message": (
            "Проверена {vendor} {product} на {drive}. GUID записан через штатный драйвер Windows."
        ),
    },
}


def detect_system_language() -> str:
    if os.name == "nt":
        try:
            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if language_id & 0x3FF == 0x19:  # LANG_RUSSIAN
                return "ru"
            return "en"
        except (AttributeError, OSError, ValueError):
            pass
    language = (locale.getlocale()[0] or "").lower()
    return "ru" if language.startswith("ru") else "en"


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
