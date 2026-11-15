"""Checks against real card databases recorded from a Sony a6500.

The snapshots contain GPS data from the owner's camera, so they are not
part of the repository.  Set CAMERA_CARD_SNAPSHOTS to the folder that holds
them (default: Desktop\\SD Backup); the tests are skipped when it is absent.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sony_card_db as m  # noqa: E402

SNAPSHOTS = Path(
    os.environ.get("CAMERA_CARD_SNAPSHOTS", Path.home() / "Desktop" / "SD Backup")
)
CET = dt.timezone(dt.timedelta(hours=1))


def load(name: str) -> tuple[bytes, bytes, bytes]:
    folder = SNAPSHOTS / name / "AVF_INFO"
    return tuple(  # type: ignore[return-value]
        (folder / file).read_bytes() for file in ("AVIN0001.INP", "AVIN0001.BNP", "PRV00001.BIN")
    )


@unittest.skipUnless((SNAPSHOTS / "card" / "AVF_INFO").is_dir(), "card snapshots not available")
class RealCardTests(unittest.TestCase):
    def test_every_snapshot_reads_and_validates(self) -> None:
        expected = {
            "card": (16, 0),
            "after-camera-delete": (15, 0),
            "after-delete-all-and-video": (0, 1),
            "new-photos": (6, 2),
        }
        for name, (photos, videos) in expected.items():
            with self.subTest(snapshot=name):
                db = m.SonyCardDatabase(*load(name))
                kinds = [item.kind for item in db.items]
                self.assertEqual((kinds.count("photo"), kinds.count("video")), (photos, videos))

    def test_replaying_the_cameras_deletion_gives_the_cameras_bytes(self) -> None:
        db = m.SonyCardDatabase(*load("card"))
        inp_camera, _bnp, prv_camera = load("after-camera-delete")
        db.delete(
            ["DCIM/100MSDCF/DSC04089.ARW"],
            dt.datetime(2026, 11, 15, 18, 30, 41, tzinfo=CET),
            _allow_partial=True,
        )
        inp, bnp, prv = db.to_bytes()
        self.assertEqual(inp, bnp)
        freed = set(range(0xAC7, 0xAD1))
        differences = [
            offset
            for offset in range(len(inp))
            if inp[offset] != inp_camera[offset]
            # The camera also edits a few bytes inside records it frees;
            # free units are never read, only their state byte matters.
            and not (
                (offset - m.UNIT_BASE) // m.UNIT in freed
                and (offset - m.UNIT_BASE) % m.UNIT != 0
            )
        ]
        self.assertEqual(differences, [])
        # Only the newest-photo summary differs; it is cleared, not
        # recomputed, when every photo is moved.
        prv_differences = [o for o in range(len(prv)) if prv[o] != prv_camera[o]]
        self.assertEqual(prv_differences, [0x31, 0x32, 0x33])

    def test_deleting_every_photo_matches_the_camera(self) -> None:
        db = m.SonyCardDatabase(*load("card"))
        db.delete(
            [item.path for item in db.items],
            dt.datetime(2026, 11, 15, 18, 55, 22, tzinfo=CET),
        )
        camera = m.SonyCardDatabase(*load("after-delete-all-and-video"))
        self.assertEqual(db.items, [])
        for top in (0x921, 0x9AE):
            ours, theirs = db.records[top], camera.records[top]
            a = bytearray(ours.payload[: ours.length])
            b = bytearray(theirs.payload[: theirs.length])
            for data in (a, b):
                data[m.MODIFIED_OFFSET : m.MODIFIED_OFFSET + 4] = b"\0\0\0\0"
            self.assertEqual(a, b)
        _inp, _bnp, prv = db.to_bytes()
        for start, end in ((0x28, 0x3C), (0xE0, 0xE4), (0x134, 0x138)):
            self.assertEqual(prv[start:end], camera.prv[start:end])

    def test_moving_everything_leaves_a_valid_empty_database(self) -> None:
        db = m.SonyCardDatabase(*load("new-photos"))
        db.delete([item.path for item in db.items], dt.datetime(2026, 11, 16, 12, 0, tzinfo=CET))
        reopened = m.SonyCardDatabase(*db.to_bytes())
        self.assertEqual(reopened.items, [])

    def test_partial_moves_are_refused(self) -> None:
        db = m.SonyCardDatabase(*load("new-photos"))
        with self.assertRaises(m.DatabaseError):
            db.delete(["DCIM/100MSDCF/DSC04090.ARW"], dt.datetime.now(CET))

    def test_inconsistent_database_is_refused(self) -> None:
        inp, bnp, prv = load("new-photos")
        with self.assertRaises(m.DatabaseError):
            m.SonyCardDatabase(inp, bnp[:-1] + b"\x01", prv)
        broken = bytearray(prv)
        broken[m.PRV_PHOTO_COUNT] += 1
        with self.assertRaises(m.DatabaseError):
            m.SonyCardDatabase(inp, bnp, bytes(broken))


if __name__ == "__main__":
    unittest.main()
