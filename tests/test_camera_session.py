import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import camera_session as cs  # noqa: E402


def info(name, size=10, folder=False, captured="20261115T190518"):
    return SimpleNamespace(
        filename=name, objectCompressedSize=size, captureDateStr=captured, objectFormat=0x3801,
        associationType=cs.MTP_OBJASSOC_GenericFolder if folder else 0,
    )


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.calls: list[str] = []
        self.ops: list[int] = []
        A = cs.A
        self.objects = {1: info("100MSDCF", folder=True), 2: info("DSC00001.ARW", 25, captured="20261115T190000"),
                        3: info("DSC00002.JPG", 7, captured="20261115T190500")}
        record = lambda name: (lambda *a, **k: self.calls.append(name))
        patches = [
            mock.patch.object(A, "startMtpSession", side_effect=record("start")),
            mock.patch.object(A, "selectMtpStorageId", side_effect=record("storage")),
            mock.patch.object(A, "getMtpStorageInfo", side_effect=record("storage_info")),
            mock.patch.object(A, "notifyCameraUserTransferSessionStarting", side_effect=record("sending")),
            mock.patch.object(A, "endMtpSession", side_effect=record("end")),
            mock.patch.object(A, "closeSockets", side_effect=record("sockets")),
            mock.patch.object(A, "getMtpObjectHandles", return_value=[1, 2, 3]),
            mock.patch.object(A, "getMtpObjectInfo", side_effect=lambda h: self.objects[h]),
            mock.patch.object(cs.mtpwifi, "execMtpOp", side_effect=self._exec),
            mock.patch.object(cs, "KEEPALIVE_SECONDS", 0.2),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        A.g.mtpDeviceInfo = SimpleNamespace(modelStr="ILCE-6500")
        A.g.storageId = 0x10001
        self.content = {2: bytes(range(25)), 3: b"jpegdat"}

    def _exec(self, sock, op, args=b"", *rest, **kw):
        self.ops.append(op)
        if op == cs.MTP_OP_GetPartialObject:
            handle, offset, count = cs.struct.unpack("<III", args)
            return SimpleNamespace(dataReceived=self.content[handle][offset:offset + count])
        if op == cs.MTP_OP_GetThumb:
            return SimpleNamespace(dataReceived=b"\xff\xd8thumb")
        return SimpleNamespace(dataReceived=b"")

    def _session(self) -> cs.CameraSession:
        session = cs.CameraSession(work_dir=str(self.dir / "work"))
        session.connect()
        self.addCleanup(session.close)
        return session

    def test_connect_runs_airmtps_session_start_in_order(self) -> None:
        session = self._session()
        self.assertEqual(self.calls, ["start", "storage", "storage_info", "sending"])
        self.assertEqual(session.camera_model, "ILCE-6500")
        self.assertEqual(cs.A.g.args["initcmdreq_guid"], cs.PAIRING_GUID)

    def test_list_skips_folders_and_puts_newest_first(self) -> None:
        names = [f.name for f in self._session().list_files()]
        self.assertEqual(names, ["DSC00002.JPG", "DSC00001.ARW"])

    def test_thumbnail(self) -> None:
        session = self._session()
        self.assertEqual(session.thumbnail(session.list_files()[0]), b"\xff\xd8thumb")

    def test_download_in_pieces(self) -> None:
        session = self._session()
        raw = next(f for f in session.list_files() if f.is_raw)
        with mock.patch.object(cs, "CHUNK_BYTES", 10):
            seen = []
            path = session.download(raw, str(self.dir / "out" / raw.name), lambda d, t: seen.append(d))
        self.assertEqual(Path(path).read_bytes(), bytes(range(25)))
        self.assertEqual(seen, [10, 20, 25])
        self.assertFalse(Path(path + ".part").exists())

    def test_failed_download_leaves_no_part_file(self) -> None:
        session = self._session()
        raw = next(f for f in session.list_files() if f.is_raw)
        self.content[2] = b""  # camera returns nothing
        target = str(self.dir / "out" / raw.name)
        with self.assertRaises(cs.SessionError):
            session.download(raw, target)
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_session_is_kept_alive_while_the_user_chooses(self) -> None:
        self._session()
        time.sleep(1.6)
        self.assertGreaterEqual(self.ops.count(cs.MTP_OP_GetDeviceInfo), 1)

    def test_close_sends_the_camera_to_sleep(self) -> None:
        session = self._session()
        session.close()
        self.assertEqual(cs.A.g.args["camerasleepwhendone"], "yes")
        self.assertIn("end", self.calls)
        self.assertFalse(session.connected)

    def test_unreachable_camera_raises_a_session_error(self) -> None:
        with mock.patch.object(cs.A, "startMtpSession", side_effect=OSError("timed out")):
            session = cs.CameraSession(work_dir=str(self.dir / "work"))
            with self.assertRaises(cs.SessionError):
                session.connect()
        self.assertFalse(session.connected)


if __name__ == "__main__":
    unittest.main()
