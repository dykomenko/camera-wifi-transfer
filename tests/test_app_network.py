from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


MODULE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODULE_DIRECTORY))

import app  # noqa: E402


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
        with mock.patch.object(app.socket, "socket", return_value=probe):
            self.assertEqual(app.primary_lan_ipv4(), "192.0.2.55")
        self.assertEqual(probe.connected_to, ("192.0.2.1", 9))

    def test_primary_lan_ipv4_falls_back_when_route_is_unavailable(self) -> None:
        probe = _RouteProbe(error=OSError("no route"))
        with mock.patch.object(app.socket, "socket", return_value=probe):
            self.assertIsNone(app.primary_lan_ipv4())


if __name__ == "__main__":
    unittest.main()
