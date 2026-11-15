"""End-to-end move on a fake card built from a recorded a6500 database.

The database files come from the owner's local snapshots (they contain GPS
data and are not in the repository); media files are small stand-ins with
the real names.  Skipped when the snapshots are absent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import card_mover_core as core  # noqa: E402
import sony_card_db  # noqa: E402

SNAPSHOT = Path(
    os.environ.get("CAMERA_CARD_SNAPSHOTS", Path.home() / "Desktop" / "SD Backup")
) / "new-photos"
CET = dt.timezone(dt.timedelta(hours=1))


@unittest.skipUnless((SNAPSHOT / "AVF_INFO").is_dir(), "card snapshots not available")
class MoveAllTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = os.path.join(temp.name, "card")
        self.dest = os.path.join(temp.name, "photos")
        for folder in ("AVF_INFO", os.path.join("PRIVATE", "M4ROOT"), os.path.join("PRIVATE", "SONY")):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        for name in ("AVIN0001.INP", "AVIN0001.BNP", "PRV00001.BIN"):
            shutil.copy(SNAPSHOT / "AVF_INFO" / name, os.path.join(self.root, "AVF_INFO", name))
        shutil.copy(SNAPSHOT / "PRIVATE" / "M4ROOT" / "MEDIAPRO.XML", os.path.join(self.root, core.MEDIAPRO))
        self.media = {}
        for folder, names in (
            (os.path.join("DCIM", "100MSDCF"), [f"DSC0{n}.ARW" for n in range(4090, 4096)]),
            (os.path.join(core.M4ROOT, "CLIP"), ["C0001.MP4", "C0001M01.XML", "C0002.MP4", "C0002M01.XML"]),
            (os.path.join(core.M4ROOT, "THMBNL"), ["C0001T01.JPG", "C0002T01.JPG"]),
        ):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
            for name in names:
                data = (name * 1000).encode()
                with open(os.path.join(self.root, folder, name), "wb") as handle:
                    handle.write(data)
                self.media[name] = hashlib.sha256(data).hexdigest()
        patcher = mock.patch.object(core, "backup_root", return_value=os.path.join(temp.name, "backups"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _move(self, cancel: threading.Event | None = None) -> core.MoveResult:
        card = core.open_card(self.root)
        return core.move_all(
            card, self.dest, by_date=False, progress=lambda *a: None,
            cancel=cancel or threading.Event(), now=lambda: dt.datetime(2026, 11, 16, 12, 0, tzinfo=CET),
        )

    def test_moves_everything_and_leaves_an_empty_valid_card(self) -> None:
        result = self._move()
        copied = {os.path.basename(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in result.copied}
        expected = {k: v for k, v in self.media.items() if not k.endswith("T01.JPG")}
        self.assertEqual(copied, expected)
        self.assertFalse(os.path.exists(os.path.join(self.root, "DCIM")))
        self.assertEqual(os.listdir(os.path.join(self.root, core.M4ROOT, "CLIP")), [])
        after = core.open_card(self.root)
        self.assertEqual(after.files, [])
        mediapro = Path(self.root, core.MEDIAPRO).read_bytes()
        self.assertEqual(len(mediapro), 2213)
        self.assertNotIn(b"<Material", mediapro)
        for name in ("AVIN0001.INP", "AVIN0001.BNP", "PRV00001.BIN", "MEDIAPRO.XML"):
            self.assertTrue(os.path.isfile(os.path.join(result.backup_folder, name)))

    def test_cancel_before_the_card_is_touched_changes_nothing(self) -> None:
        cancel = threading.Event()
        cancel.set()
        before = {p: Path(p).read_bytes() for p in Path(self.root).rglob("*") if p.is_file()}
        with self.assertRaises(core.CardError):
            self._move(cancel)
        after = {p: Path(p).read_bytes() for p in Path(self.root).rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_file_not_in_the_database_blocks_the_move(self) -> None:
        Path(self.root, "DCIM", "100MSDCF", "DSC09999.ARW").write_bytes(b"x")
        with self.assertRaises(core.CardError) as caught:
            core.open_card(self.root)
        self.assertEqual(caught.exception.args[0], "card_and_database_differ")

    def test_a_bad_copy_leaves_the_card_untouched(self) -> None:
        before = {p: Path(p).read_bytes() for p in Path(self.root).rglob("*") if p.is_file()}
        real_hash = core._hash_file
        calls = {"n": 0}

        def corrupt(path, cancel, progress):
            calls["n"] += 1
            return "0" * 64 if path.endswith(".part") and calls["n"] == 1 else real_hash(path, cancel, progress)

        with mock.patch.object(core, "_hash_file", side_effect=corrupt):
            with self.assertRaises(core.CardError) as caught:
                self._move()
        self.assertEqual(caught.exception.args[0], "copy_mismatch")
        after = {p: Path(p).read_bytes() for p in Path(self.root).rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_existing_identical_file_is_not_duplicated(self) -> None:
        os.makedirs(self.dest)
        Path(self.dest, "DSC04090.ARW").write_bytes(("DSC04090.ARW" * 1000).encode())
        result = self._move()
        self.assertIn(os.path.join(self.dest, "DSC04090.ARW"), result.already_there)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "DSC04090 (2).ARW")))


if __name__ == "__main__":
    unittest.main()
