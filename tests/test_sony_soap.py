"""Offline tests for the Sony Send to Smartphone SOAP client."""

from __future__ import annotations

from html import escape
from io import BytesIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Callable, Mapping
import unittest
from unittest import mock
from urllib.request import Request


MODULE_DIRECTORY = Path(__file__).resolve().parents[1]
if str(MODULE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(MODULE_DIRECTORY))

import sony_soap  # noqa: E402  (path is intentionally installed above)
from sony_soap import (  # noqa: E402
    DownloadError,
    MediaItem,
    MediaResource,
    ProtocolError,
    SonySoapClient,
    UnsafeResourceUrlError,
)


SOAP_EMPTY_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body />
</s:Envelope>
"""


class FakeResponse:
    """Small urllib response double backed by in-memory bytes."""

    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = BytesIO(body)
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        """Read up to ``amount`` response bytes."""

        return self._body.read(amount)

    def close(self) -> None:
        """Mark the response as closed."""

        self.closed = True
        self._body.close()


class FakeOpener:
    """Record requests and dispatch them to a test-local handler."""

    def __init__(self, handler: Callable[[Request], FakeResponse]) -> None:
        self.handler = handler
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    def __call__(self, request: Request, *, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.handler(request)


def didl_document(children: str = "") -> str:
    """Build a DIDL-Lite document used inside a SOAP Result string."""

    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{children}"
        "</DIDL-Lite>"
    )


def browse_response(didl: str, number_returned: int, total_matches: int) -> bytes:
    """Wrap an escaped DIDL-Lite document in a UPnP Browse SOAP response."""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
      <Result>{escape(didl)}</Result>
      <NumberReturned>{number_returned}</NumberReturned>
      <TotalMatches>{total_matches}</TotalMatches>
      <UpdateID>1</UpdateID>
    </u:BrowseResponse>
  </s:Body>
</s:Envelope>""".encode("utf-8")


def request_body(request: Request) -> str:
    """Decode one captured request body."""

    return (request.data or b"").decode("utf-8")


class ProbeAndTransferTests(unittest.TestCase):
    """Descriptor and Sony XPush action tests."""

    def test_probe_parses_identity_and_control_urls(self) -> None:
        descriptor = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>ILCE-6500</friendlyName>
    <manufacturer>Sony Corporation</manufacturer>
    <modelName>ILCE-6500</modelName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-sony-com:service:XPushList:1</serviceType>
        <controlURL>/custom/XPushList</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <controlURL>/custom/ContentDirectory</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""
        opener = FakeOpener(lambda _request: FakeResponse(descriptor))
        client = SonySoapClient(opener=opener, timeout=2.5)

        found = client.probe()

        self.assertEqual(found.manufacturer, "Sony Corporation")
        self.assertEqual(found.model_name, "ILCE-6500")
        self.assertEqual(found.friendly_name, "ILCE-6500")
        self.assertEqual(
            found.xpush_control_url,
            "http://192.168.122.1:64321/custom/XPushList",
        )
        self.assertEqual(
            found.content_directory_control_url,
            "http://192.168.122.1:64321/custom/ContentDirectory",
        )
        self.assertEqual(opener.requests[0].get_method(), "GET")
        self.assertTrue(opener.requests[0].full_url.endswith("/DmsDescPush.xml"))
        self.assertEqual(opener.timeouts, [2.5])

    def test_probe_rejects_a_non_sony_descriptor(self) -> None:
        descriptor = b"""<root><device><manufacturer>Not A Camera</manufacturer>
<modelName>Router</modelName></device></root>"""
        client = SonySoapClient(opener=FakeOpener(lambda _request: FakeResponse(descriptor)))

        with self.assertRaises(ProtocolError):
            client.probe()

    def test_transfer_start_and_end_send_expected_actions(self) -> None:
        opener = FakeOpener(lambda _request: FakeResponse(SOAP_EMPTY_RESPONSE))
        client = SonySoapClient(opener=opener)

        client.start_transfer()
        client.end_transfer()

        self.assertEqual(len(opener.requests), 2)
        start, end = opener.requests
        start_headers = {key.casefold(): value for key, value in start.header_items()}
        end_headers = {key.casefold(): value for key, value in end.header_items()}
        self.assertEqual(start.get_method(), "POST")
        self.assertIn("#X_TransferStart", start_headers["soapaction"])
        self.assertIn("#X_TransferEnd", end_headers["soapaction"])
        self.assertIn("<u:X_TransferStart", request_body(start))
        self.assertIn("<u:X_TransferEnd", request_body(end))
        self.assertIn("<ErrCode>0</ErrCode>", request_body(end))


class BrowseTests(unittest.TestCase):
    """SOAP Browse, pagination, DIDL-Lite and quality selection tests."""

    def test_browse_all_paginates_recurses_and_selects_best_resources(self) -> None:
        first_page = didl_document(
            """
<item id="photo-1" parentID="PushRoot">
  <dc:title>DSC0001.JPG</dc:title>
  <res size="100" resolution="640x480" protocolInfo="image/jpeg:JPEG_SM">http://192.168.122.1:64321/small.jpg</res>
  <res size="1000" resolution="6000x4000" protocolInfo="image/jpeg:JPEG_LRG">http://192.168.122.1:64321/original.jpg</res>
</item>
<container id="album-1" parentID="PushRoot"><dc:title>2026-08-16</dc:title></container>
"""
        )
        second_page = didl_document(
            """
<item id="photo-2" parentID="PushRoot">
  <dc:title>DSC0002.JPG</dc:title>
  <res size="800">http://192.168.122.1:64321/two.jpg</res>
</item>
"""
        )
        album_page = didl_document(
            """
<item id="raw-1" parentID="album-1">
  <dc:title>DSC0003.ARW</dc:title>
  <res protocolInfo="image/jpeg:JPEG_TN">http://192.168.122.1:64321/thumb.jpg</res>
  <res protocolInfo="image/jpeg:JPEG_LRG">http://192.168.122.1:64321/raw-preview.jpg</res>
</item>
"""
        )

        def handler(request: Request) -> FakeResponse:
            body = request_body(request)
            if "X_TransferStart" in body:
                return FakeResponse(SOAP_EMPTY_RESPONSE)
            if "<ObjectID>PushRoot</ObjectID>" in body and "<StartingIndex>0</StartingIndex>" in body:
                return FakeResponse(browse_response(first_page, 2, 3))
            if "<ObjectID>PushRoot</ObjectID>" in body and "<StartingIndex>2</StartingIndex>" in body:
                return FakeResponse(browse_response(second_page, 1, 3))
            if "<ObjectID>album-1</ObjectID>" in body:
                return FakeResponse(browse_response(album_page, 1, 1))
            self.fail(f"unexpected Browse request: {body}")

        opener = FakeOpener(handler)
        client = SonySoapClient(opener=opener)

        items = client.browse_all("PushRoot")

        self.assertEqual([item.object_id for item in items], ["photo-1", "photo-2", "raw-1"])
        self.assertEqual(items[0].best_resource().url.rsplit("/", 1)[-1], "original.jpg")
        self.assertEqual(items[2].best_resource().url.rsplit("/", 1)[-1], "raw-preview.jpg")
        self.assertEqual(len(opener.requests), 4)

    def test_browse_all_falls_back_from_pushroot_to_photoroot(self) -> None:
        seen_bodies: list[str] = []

        def handler(request: Request) -> FakeResponse:
            body = request_body(request)
            if "X_TransferStart" in body:
                return FakeResponse(SOAP_EMPTY_RESPONSE)
            seen_bodies.append(body)
            if "<ObjectID>PushRoot</ObjectID>" in body:
                return FakeResponse(b"<error />", status=500)
            if "<ObjectID>PhotoRoot</ObjectID>" in body:
                return FakeResponse(browse_response(didl_document(), 0, 0))
            self.fail(f"unexpected request: {body}")

        client = SonySoapClient(opener=FakeOpener(handler))

        self.assertEqual(client.browse_all(), [])
        self.assertEqual(len(seen_bodies), 2)
        self.assertIn("<ObjectID>PushRoot</ObjectID>", seen_bodies[0])
        self.assertIn("<ObjectID>PhotoRoot</ObjectID>", seen_bodies[1])


class DownloadTests(unittest.TestCase):
    """Safe path, partial file, progress, and origin validation tests."""

    def test_download_uses_safe_name_part_file_and_atomic_replace(self) -> None:
        content = b"jpeg-bytes"
        opener = FakeOpener(
            lambda request: FakeResponse(
                content,
                headers={"content-length": str(len(content))},
            )
        )
        client = SonySoapClient(opener=opener)
        item = MediaItem(
            "1",
            "PushRoot",
            r"..\outside/DSC:0001.JPG",
            (
                MediaResource(
                    "http://192.168.122.1:64321/content/DSC0001.JPG",
                    size=len(content),
                ),
            ),
        )
        progress: list[tuple[int, int | None]] = []

        with TemporaryDirectory() as temporary, mock.patch(
            "sony_soap.os.replace", wraps=os.replace
        ) as replace:
            destination = Path(temporary)
            result = client.download(
                item,
                destination,
                lambda written, expected: progress.append((written, expected)),
            )

            self.assertEqual(result.parent, destination)
            self.assertEqual(result.name, "DSC_0001.JPG")
            self.assertEqual(result.read_bytes(), content)
            self.assertFalse((destination / "DSC_0001.JPG.part").exists())
            replace.assert_called_once()
            self.assertEqual(progress[-1], (len(content), len(content)))
            self.assertEqual(opener.requests[0].get_method(), "GET")

    def test_download_accepts_an_explicit_output_file(self) -> None:
        content = b"explicit-file"
        opener = FakeOpener(
            lambda _request: FakeResponse(
                content, headers={"Content-Length": str(len(content))}
            )
        )
        client = SonySoapClient(opener=opener)
        item = MediaItem(
            "explicit",
            "PushRoot",
            "camera-name.JPG",
            (
                MediaResource(
                    "http://192.168.122.1:64321/explicit.jpg", size=len(content)
                ),
            ),
        )

        with TemporaryDirectory() as temporary:
            requested_path = Path(temporary) / "chosen-name.JPG"
            result = client.download(item, requested_path)

            self.assertEqual(result, requested_path)
            self.assertEqual(result.read_bytes(), content)

    def test_short_download_leaves_only_part_file(self) -> None:
        opener = FakeOpener(
            lambda _request: FakeResponse(b"abc", headers={"Content-Length": "5"})
        )
        client = SonySoapClient(opener=opener)
        item = MediaItem(
            "2",
            "PushRoot",
            "DSC0002.JPG",
            (MediaResource("http://192.168.122.1:64321/two.jpg", size=5),),
        )

        with TemporaryDirectory() as temporary:
            destination = Path(temporary)
            with self.assertRaises(DownloadError):
                client.download(item, destination)

            self.assertFalse((destination / "DSC0002.JPG").exists())
            self.assertEqual((destination / "DSC0002.JPG.part").read_bytes(), b"abc")

    def test_download_rejects_off_camera_resource_before_network_access(self) -> None:
        def forbidden_network(_request: Request) -> FakeResponse:
            self.fail("unsafe URL must be rejected before opening a connection")

        client = SonySoapClient(opener=FakeOpener(forbidden_network))
        item = MediaItem(
            "3",
            "PushRoot",
            "DSC0003.JPG",
            (MediaResource("http://127.0.0.1:8080/private"),),
        )

        with TemporaryDirectory() as temporary, self.assertRaises(UnsafeResourceUrlError):
            client.download(item, temporary)


if __name__ == "__main__":
    unittest.main()
