import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wifi_transfer  # noqa: E402
from localization import Translator  # noqa: E402
from sony_soap import TransportError  # noqa: E402


class _RouteProbe:
    def __init__(self, address: str = "192.0.2.55", error: OSError | None = None) -> None:
        self.address = address
        self.error = error
        self.connected_to: tuple[str, int] | None = None

    def __enter__(self) -> "_RouteProbe":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def connect(self, target: tuple[str, int]) -> None:
        self.connected_to = target
        if self.error:
            raise self.error

    def getsockname(self) -> tuple[str, int]:
        return (self.address, 54321)


class NetworkSelectionTests(unittest.TestCase):
    def test_primary_lan_ipv4_uses_windows_default_route(self) -> None:
        probe = _RouteProbe()
        with mock.patch.object(wifi_transfer.socket, "socket", return_value=probe):
            self.assertEqual(wifi_transfer.primary_lan_ipv4(), "192.0.2.55")
        self.assertEqual(probe.connected_to, ("192.0.2.1", 9))

    def test_primary_lan_ipv4_falls_back_when_route_is_unavailable(self) -> None:
        probe = _RouteProbe(error=OSError("no route"))
        with mock.patch.object(wifi_transfer.socket, "socket", return_value=probe):
            self.assertIsNone(wifi_transfer.primary_lan_ipv4())


class CameraWaitTests(unittest.TestCase):
    """Auto-start keeps looking for a camera that is not ready yet."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.destination = Path(temp.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LOCALAPPDATA": temp.name}),
            mock.patch.object(wifi_transfer, "CAMERA_RETRY_PAUSE_SECONDS", 0.01),
            mock.patch.object(wifi_transfer, "backend_command", return_value=["backend"]),
            mock.patch.object(wifi_transfer, "primary_lan_ipv4", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = wifi_transfer.WifiTransfer(Translator("en"), queue.Queue())

    def _events(self) -> list[tuple[str, object]]:
        events = []
        while True:
            try:
                events.append(self.window.events.get_nowait())
            except queue.Empty:
                return events

    def _logs(self, events: list[tuple[str, object]]) -> list[str]:
        return [str(payload) for kind, payload in events if kind == "log"]

    def _run_network(self, results: list[tuple[int, int, int, int]], deadline: float | None):
        with mock.patch.object(self.window, "_run_backend_once", side_effect=results) as run:
            self.window._ptpip_worker(self.destination, deadline)
        events = self._events()
        return run, events, events[-1]

    def test_network_mode_retries_until_the_camera_appears(self) -> None:
        not_found = (wifi_transfer.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)
        run, events, done = self._run_network(
            [not_found, not_found, (0, 3, 2, 5)], time.monotonic() + 60
        )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(done, ("done", {"success": True, "downloaded": 3, "skipped": 2}))
        waits = [line for line in self._logs(events) if "keep looking" in line]
        self.assertEqual(len(waits), 1, "the wait is explained once, not on every retry")

    def test_manual_start_does_not_wait(self) -> None:
        run, _events, done = self._run_network(
            [(wifi_transfer.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], None
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_gives_up_when_the_wait_is_over(self) -> None:
        run, events, done = self._run_network(
            [(wifi_transfer.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], time.monotonic()
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])
        self.assertTrue(any("not found within" in line for line in self._logs(events)))

    def test_other_failures_are_not_retried(self) -> None:
        run, _events, done = self._run_network([(1, 0, 0, 0)], time.monotonic() + 60)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_stop_ends_the_wait(self) -> None:
        self.window.cancel_requested.set()
        run, _events, done = self._run_network(
            [(wifi_transfer.BACKEND_ERRNO_COULDNT_CONNECT, 0, 0, 0)], time.monotonic() + 60
        )
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_broken_transfer_reconnects_and_counts_each_file_once(self) -> None:
        # 3 of 10 files arrive, Wi-Fi drops; the stuck camera ignores one
        # session; after Cancel on the camera the rest is copied and the
        # 3 earlier files are found in the folder.
        timed_out = 10060
        run, events, done = self._run_network(
            [
                (timed_out, 3, 1, 10),
                (timed_out, 0, 0, 0),
                (0, 6, 4, 10),
            ],
            None,  # also for a manual start
        )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(done, ("done", {"success": True, "downloaded": 9, "skipped": 1}))
        lost = [line for line in self._logs(events) if "connection to the camera was lost" in line]
        self.assertEqual(len(lost), 1)

    def test_local_file_errors_are_not_retried(self) -> None:
        run, _events, done = self._run_network([(5006, 2, 0, 10)], None)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])

    def test_reconnect_wait_gives_up_eventually(self) -> None:
        with mock.patch.object(wifi_transfer, "CAMERA_WAIT_SECONDS", 0):
            run, events, done = self._run_network([(10060, 3, 0, 10)], None)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(done[1]["success"])
        self.assertEqual(done[1]["downloaded"], 3)
        logs = self._logs(events)
        self.assertTrue(any("did not reconnect" in line for line in logs))
        self.assertFalse(any("not found within" in line for line in logs))
        self.assertFalse(any("Transfer did not finish" in line for line in logs))

    def test_connection_loss_brings_the_window_up(self) -> None:
        _run, events, _done = self._run_network([(10060, 1, 0, 5), (0, 4, 1, 5)], None)
        self.assertIn(("attention", None), events)

    def test_airmtp_sony_advice_is_replaced(self) -> None:
        lines = [
            "\n",
            "airmtp v1.1 - MTP network transfer of images/movies for cameras [GPL v3]\n",
            "Time: 11/11/26 00:16:13, Py: 3.10.6, OS: Windows\n",
            "Found camera at IP address 192.168.2.50\n",
            "\n",
            "Before running airmtp again please press 'Cancel' on your Sony camera's\n",
            "'Send to Computer' screen and select 'Send to Computer' again. Failing to\n",
            "do this will cause the next airmtp session to not negotiate successfully.\n",
            "Number of attempts (1) has reached maximum configured value - exiting\n",
        ]
        process = mock.Mock(stdout=iter(lines))
        process.wait.return_value = 10060
        with mock.patch.object(wifi_transfer.subprocess, "Popen", return_value=process):
            self.window._run_backend_once(["backend"], set())
        logs = " ".join(self._logs(self._events()))
        self.assertIn("192.168.2.50", logs)
        self.assertNotIn("airmtp", logs)
        self.assertNotIn("Number of attempts", logs)
        self.assertNotIn("Py: 3.10.6", logs)

    def test_backend_reads_the_file_list_fresh_every_session(self) -> None:
        with mock.patch.object(self.window, "_run_backend_once", return_value=(0, 0, 0, 0)) as run:
            self.window._ptpip_worker(self.destination, None)
        command = run.call_args[0][0]
        self.assertEqual(command[command.index("--mtpobjcache") + 1], "disabled")
        self.assertEqual(command[command.index("--downloadhistory") + 1], "ignore")

    def test_direct_mode_retries_until_the_camera_appears(self) -> None:
        client = mock.Mock()
        client.probe.side_effect = [
            TransportError("unreachable"),
            TransportError("unreachable"),
            mock.Mock(friendly_name="ILCE-6500", model_name="ILCE-6500"),
        ]
        client.browse_all.return_value = []
        with mock.patch.object(wifi_transfer, "SonySoapClient", return_value=client):
            self.window._direct_worker(self.destination, time.monotonic() + 60)
        self.assertEqual(client.probe.call_count, 3)
        self.assertTrue(self._events()[-1][1]["success"])


if __name__ == "__main__":
    unittest.main()
