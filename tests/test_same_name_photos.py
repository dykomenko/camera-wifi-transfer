"""A file with the camera file's name already in the folder is skipped only
when it is the same photo; a different photo with that name is kept too."""

import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "airmtp"))

import airmtp  # noqa: E402
import wifi_transfer  # noqa: E402
from backend_output import BackendOutputParser  # noqa: E402
from localization import Translator  # noqa: E402


class SameSizedCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def _file(self, name: str, size: int) -> str:
        path = self.folder / name
        path.write_bytes(b"x" * size)
        return str(path)

    def test_same_photo_is_recognised(self) -> None:
        path = self._file("DSC01234.ARW", 100)
        self.assertEqual(airmtp.findSameSizedLocalCopy(path, 100), path)

    def test_different_photo_with_the_same_name_is_not_skipped(self) -> None:
        path = self._file("DSC01234.ARW", 100)
        self.assertIsNone(airmtp.findSameSizedLocalCopy(path, 250))

    def test_earlier_renamed_copy_is_found_on_the_next_sync(self) -> None:
        path = self._file("DSC01234.ARW", 100)
        copy = self._file("DSC01234-new-1.ARW", 250)
        self.assertEqual(airmtp.findSameSizedLocalCopy(path, 250), copy)
        # The copy airmtp would make next is a new name, never an overwrite.
        self.assertEqual(os.path.basename(airmtp.generateUniqueFilename(path)), "DSC01234-new-2.ARW")

    def test_names_with_glob_characters(self) -> None:
        path = self._file("clip[1].MP4", 10)
        copy = self._file("clip[1]-new-1.MP4", 20)
        self.assertEqual(airmtp.findSameSizedLocalCopy(path, 20), copy)

    def test_videos_of_4_gib_and_more(self) -> None:
        # MTP reports 0xFFFFFFFF as the size of objects of 4 GiB and more.
        path = self._file("C0001.MP4", 1)
        with mock.patch.object(airmtp.os.path, "getsize", return_value=5 * 2**30):
            self.assertEqual(airmtp.findSameSizedLocalCopy(path, 0xFFFFFFFF), path)
        self.assertIsNone(airmtp.findSameSizedLocalCopy(path, 0xFFFFFFFF))

    def test_option_is_accepted_by_the_backend(self) -> None:
        source = (ROOT / "vendor" / "airmtp" / "airmtp.py").read_text(encoding="utf-8")
        self.assertIn("'skipsame'", source)


class RenamedEventTests(unittest.TestCase):
    def test_backend_event_is_parsed(self) -> None:
        line = 'CWFT\tRENAMED\t"DSC01234.ARW"\t"DSC01234-new-1.ARW"\n'
        parsed = BackendOutputParser().parse(line)
        self.assertEqual(parsed.renamed, ("DSC01234.ARW", "DSC01234-new-1.ARW"))

    def test_rename_is_explained_in_the_log(self) -> None:
        transfer = wifi_transfer.WifiTransfer(Translator("en"), queue.Queue())
        lines = ['CWFT\tRENAMED\t"DSC01234.ARW"\t"DSC01234-new-1.ARW"\n']
        process = mock.Mock(stdout=iter(lines))
        process.wait.return_value = 0
        with mock.patch.object(wifi_transfer.subprocess, "Popen", return_value=process):
            transfer._run_backend_once(["backend"], set())
        logs = []
        while not transfer.events.empty():
            kind, payload = transfer.events.get_nowait()
            if kind == "log":
                logs.append(payload)
        self.assertIn("DSC01234.ARW is already in the folder but is a different photo, "
                      "so it was saved as DSC01234-new-1.ARW.", logs)

    def test_transfers_use_the_size_aware_check(self) -> None:
        transfer = wifi_transfer.WifiTransfer(Translator("en"), queue.Queue())
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            wifi_transfer, "backend_command", return_value=["backend"]
        ), mock.patch.object(wifi_transfer, "primary_lan_ipv4", return_value=None), mock.patch.object(
            transfer, "_run_backend_once", return_value=(0, 0, 0, 0)
        ) as run:
            transfer._ptpip_worker(Path(folder), None)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--ifexists") + 1], "skipsame")


if __name__ == "__main__":
    unittest.main()
