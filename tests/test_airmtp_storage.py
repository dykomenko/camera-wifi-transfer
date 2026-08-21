from __future__ import annotations

from pathlib import Path
import struct
import sys
import unittest


AIR_MTP_DIRECTORY = Path(__file__).resolve().parents[1] / "vendor" / "airmtp"
if str(AIR_MTP_DIRECTORY) not in sys.path:
    # Append so the normal installed ``six`` package keeps precedence over
    # airMTP's legacy single-file compatibility copy.
    sys.path.append(str(AIR_MTP_DIRECTORY))

import airmtp  # noqa: E402


def mtp_string(value: str) -> bytes:
    if not value:
        return b"\x00"
    encoded = value.encode("utf-16le")
    return bytes((len(value) + 1,)) + encoded + b"\x00\x00"


class StorageInfoParsingTests(unittest.TestCase):
    def test_description_and_volume_label_are_separate_mtp_strings(self) -> None:
        payload = struct.pack(
            "<HHHQQI",
            3,
            2,
            0,
            64_000_000_000,
            58_000_000_000,
            2048,
        ) + mtp_string("SD Card") + mtp_string("@dykomenko")

        storage = airmtp.parseMtpStorageInfo(payload)

        self.assertEqual(storage.storageDescription, "SD Card")
        self.assertEqual(storage.volumeLabel, "@dykomenko")
        self.assertEqual(storage.maxCapacityBytes, 64_000_000_000)
        self.assertEqual(storage.freeSpaceBytes, 58_000_000_000)

    def test_empty_volume_label_is_not_confused_with_description(self) -> None:
        payload = struct.pack("<HHHQQI", 3, 2, 0, 64, 58, 2)
        payload += mtp_string("Storage Media") + mtp_string("")

        storage = airmtp.parseMtpStorageInfo(payload)

        self.assertEqual(storage.storageDescription, "Storage Media")
        self.assertEqual(storage.volumeLabel, "")


if __name__ == "__main__":
    unittest.main()
