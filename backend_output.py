from __future__ import annotations

from dataclasses import dataclass
import re


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DOWNLOAD_RE = re.compile(r'^Downloading\s+"([^"]+)":\s*(\d{1,3})%\s*$')
PROGRESS_RE = re.compile(r"^(\d{1,3})%$")
LEGACY_JOINED_RE = re.compile(r'^Downloading\s+"([^"]+)":.*?(\d{1,3})%\s*$')
GUI_EVENT_RE = re.compile(r"^CWFT\t([A-Z_]+)(?:\t(.*))?$")


def clean_backend_text(value: str) -> str:
    """Remove terminal formatting that becomes boxes in a GUI text widget."""

    value = ANSI_RE.sub("", value).replace("\r", "")
    value = CONTROL_RE.sub("", value)
    return value.strip()


@dataclass(frozen=True)
class ParsedBackendLine:
    log_text: str | None = None
    filename: str | None = None
    percent: int | None = None
    current: int | None = None
    total: int | None = None
    storage_total: int | None = None
    storage_free: int | None = None
    storage_label: str | None = None
    item_done: bool = False
    item_skipped: bool = False


class BackendOutputParser:
    """Convert airMTP's console-oriented progress stream into GUI events."""

    def __init__(self) -> None:
        self.active_filename: str | None = None

    def parse(self, raw: str) -> ParsedBackendLine:
        line = clean_backend_text(raw)
        if not line:
            return ParsedBackendLine()

        gui_event = GUI_EVENT_RE.fullmatch(line)
        if gui_event:
            kind = gui_event.group(1)
            fields = (gui_event.group(2) or "").split("\t")
            try:
                if kind == "TOTAL" and fields:
                    return ParsedBackendLine(total=max(0, int(fields[0])))
                if kind == "ITEM" and len(fields) >= 3:
                    self.active_filename = fields[2]
                    return ParsedBackendLine(
                        filename=self.active_filename,
                        percent=0,
                        current=max(0, int(fields[0])),
                        total=max(0, int(fields[1])),
                    )
                if kind == "PROGRESS" and fields and self.active_filename:
                    return ParsedBackendLine(
                        filename=self.active_filename,
                        percent=max(0, min(100, int(fields[0]))),
                    )
                if kind == "ITEM_DONE" and len(fields) >= 4:
                    self.active_filename = fields[2]
                    return ParsedBackendLine(
                        filename=self.active_filename,
                        percent=100,
                        current=max(0, int(fields[0])),
                        total=max(0, int(fields[1])),
                        item_done=True,
                        item_skipped=fields[3] != "downloaded",
                    )
                if kind == "STORAGE" and len(fields) >= 3:
                    return ParsedBackendLine(
                        storage_total=max(0, int(fields[0])),
                        storage_free=max(0, int(fields[1])),
                        storage_label=fields[2],
                    )
            except ValueError:
                return ParsedBackendLine()
            return ParsedBackendLine()

        download = DOWNLOAD_RE.fullmatch(line)
        if download:
            self.active_filename = download.group(1)
            return ParsedBackendLine(
                log_text=f'Downloading "{self.active_filename}"',
                filename=self.active_filename,
                percent=max(0, min(100, int(download.group(2)))),
            )

        progress = PROGRESS_RE.fullmatch(line)
        if progress and self.active_filename:
            return ParsedBackendLine(
                filename=self.active_filename,
                percent=max(0, min(100, int(progress.group(1)))),
            )

        # Older backend builds wrote every backspace-based percentage into one
        # buffered line. Keep this fallback so their output never floods the GUI.
        legacy = LEGACY_JOINED_RE.fullmatch(line)
        if legacy:
            self.active_filename = legacy.group(1)
            percentages = re.findall(r"(\d{1,3})%", line)
            return ParsedBackendLine(
                log_text=f'Downloading "{self.active_filename}"',
                filename=self.active_filename,
                percent=max(0, min(100, int(percentages[-1]))),
            )

        return ParsedBackendLine(log_text=line)
