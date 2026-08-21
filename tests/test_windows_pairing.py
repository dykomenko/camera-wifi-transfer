from __future__ import annotations

import ctypes
import unittest

import windows_pairing as pairing


class PairingPacketTests(unittest.TestCase):
    def test_payload_matches_reverse_engineered_sony_layout(self) -> None:
        payload = pairing.build_pairing_payload("DESKTOP")

        self.assertEqual(len(payload), 8192)
        self.assertEqual(payload[0:5], b"\x25\x01\x00\x00\x08")
        self.assertEqual(payload[17:31], "DESKTOP".encode("utf-16le"))
        self.assertEqual(payload[177:181], b"\x10\x00\x00\x00")
        self.assertEqual(payload[181:189], pairing.GUID1)
        self.assertEqual(payload[189:197], pairing.GUID2)
        self.assertEqual(payload[197:], bytes(8192 - 197))

    def test_hostname_is_bounded_to_16_utf16_bytes(self) -> None:
        payload = pairing.build_pairing_payload("TOO-LONG-HOSTNAME")
        self.assertEqual(payload[17:33], "TOO-LONG".encode("utf-16le"))

    def test_cdb_is_exactly_the_original_nine_bytes(self) -> None:
        self.assertEqual(pairing.PAIRING_CDB, b"\x7a\x04" + bytes(7))
        self.assertEqual(len(pairing.PAIRING_CDB), 9)

    def test_inquiry_parser_accepts_only_sony_dsc(self) -> None:
        raw = bytearray(96)
        raw[8:16] = b"SONY    "
        raw[16:32] = b"DSC             "
        raw[32:36] = b"1.00"
        info = pairing.parse_inquiry(bytes(raw))

        self.assertTrue(info.is_sony_camera)
        self.assertEqual(info.vendor, "SONY")
        self.assertEqual(info.product, "DSC")
        self.assertEqual(info.revision, "1.00")

    def test_windows_struct_layout_matches_spti_contract(self) -> None:
        self.assertEqual(ctypes.sizeof(pairing.SCSI_PASS_THROUGH_DIRECT), 56)
        self.assertEqual(pairing.SCSI_PASS_THROUGH_DIRECT.DataBuffer.offset, 24)
        self.assertEqual(pairing.SCSI_PASS_THROUGH_DIRECT.Cdb.offset, 36)
        self.assertGreaterEqual(
            pairing.SCSI_PASS_THROUGH_DIRECT_WITH_BUFFER.Sense.offset,
            ctypes.sizeof(pairing.SCSI_PASS_THROUGH_DIRECT),
        )


if __name__ == "__main__":
    unittest.main()
