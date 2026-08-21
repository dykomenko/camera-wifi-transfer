"""Sony ``Send to Smartphone`` UPnP/SOAP client.

The module implements the small protocol surface needed to receive photographs
from Sony cameras which expose ``DmsDescPush.xml`` while in the camera's
``Send to Smartphone`` mode.  It deliberately uses only the Python standard
library and contains no UI or Wi-Fi connection management.

The protocol exposes full-size JPEG resources.  A camera recording RAW only
normally exposes a generated JPEG resource here, not the original ARW file.
Use Sony's PTP/IP ``Send to Computer`` mode when original RAW transfer is
required.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable, Iterable, Iterator, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


DEFAULT_HOST = "192.168.122.1"
DEFAULT_PORT = 64321
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"
XPUSH_SERVICE = "urn:schemas-sony-com:service:XPushList:1"
CONTENT_DIRECTORY_SERVICE = "urn:schemas-upnp-org:service:ContentDirectory:1"
DESCRIPTOR_PATH = "/DmsDescPush.xml"
DEFAULT_XPUSH_CONTROL_PATH = "/upnp/control/XPushList"
DEFAULT_CONTENT_CONTROL_PATH = "/upnp/control/ContentDirectory"
MAX_XML_BYTES = 16 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024

ProgressCallback = Callable[[int, int | None], None]


class _HttpResponse(Protocol):
    """Structural type required from an injected ``urlopen`` replacement."""

    status: int
    headers: Mapping[str, str]

    def read(self, amount: int = -1) -> bytes:
        """Read response bytes."""

    def close(self) -> None:
        """Close the response."""


UrlOpen = Callable[..., _HttpResponse]


class SonySoapError(Exception):
    """Base exception for Sony SOAP discovery and transfer failures."""


class TransportError(SonySoapError):
    """The camera could not be reached or the HTTP exchange failed."""


class HttpStatusError(SonySoapError):
    """The camera returned a non-success HTTP status."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"camera returned HTTP {status} for {url}")
        self.status = status
        self.url = url


class ProtocolError(SonySoapError):
    """The response is not valid for the expected Sony/UPnP protocol."""


class SoapFault(SonySoapError):
    """A SOAP Fault returned by the camera."""

    def __init__(self, code: str | None, message: str | None) -> None:
        detail = ": ".join(part for part in (code, message) if part)
        super().__init__(f"camera returned SOAP fault{': ' + detail if detail else ''}")
        self.code = code
        self.message = message


class NoResourceError(SonySoapError):
    """A media item contains no downloadable resource."""


class UnsafeResourceUrlError(SonySoapError):
    """A resource URL points outside the configured camera origin."""


class DownloadError(SonySoapError):
    """A download was incomplete or could not be committed safely."""


@dataclass(frozen=True, slots=True)
class CameraDescriptor:
    """Identity and resolved control endpoints returned by ``probe()``."""

    descriptor_url: str
    manufacturer: str | None
    model_name: str | None
    friendly_name: str | None
    xpush_control_url: str
    content_directory_control_url: str


@dataclass(frozen=True, slots=True)
class MediaResource:
    """One downloadable ``res`` element from a DIDL-Lite media item."""

    url: str
    size: int | None = None
    resolution: tuple[int, int] | None = None
    protocol_info: str = ""

    @property
    def pixel_count(self) -> int:
        """Return the number of pixels described by ``resolution``."""

        if self.resolution is None:
            return 0
        return self.resolution[0] * self.resolution[1]


@dataclass(frozen=True, slots=True)
class MediaItem:
    """A photograph and the alternate resources exposed by the camera."""

    object_id: str
    parent_id: str | None
    title: str
    resources: tuple[MediaResource, ...]

    @property
    def filename(self) -> str:
        """Compatibility alias used by simple import frontends."""

        return self.title

    @property
    def size(self) -> int | None:
        """Advertised size of the selected best resource, when available."""

        return self.best_resource().size

    def best_resource(self) -> MediaResource:
        """Choose the highest-quality resource using Sony's advertised data.

        Sony normally supplies byte sizes for original JPEG resources, so the
        largest sized resource wins.  RAW-only records generally omit an
        original and advertise JPEG variants through protocol markers; in that
        case ``_ORG`` and then ``_LRG`` are preferred before resolution and
        document order are used as fallbacks.
        """

        if not self.resources:
            raise NoResourceError(f"item {self.title!r} has no resources")

        sized = [resource for resource in self.resources if resource.size is not None]
        if sized:
            return max(sized, key=lambda resource: (resource.size or 0, resource.pixel_count))

        marker_order = ("_ORG", "ORIGINAL", "_LRG", "_LARGE", "_SM", "_TN")
        for marker in marker_order:
            marked = [
                resource
                for resource in self.resources
                if marker in resource.protocol_info.upper()
            ]
            if marked:
                return max(marked, key=lambda resource: resource.pixel_count)

        resolved = [resource for resource in self.resources if resource.resolution]
        if resolved:
            return max(resolved, key=lambda resource: resource.pixel_count)

        # Sony lists increasing-quality JPEG alternatives last on cameras which
        # omit all useful attributes.
        return self.resources[-1]


@dataclass(frozen=True, slots=True)
class MediaContainer:
    """A DIDL-Lite directory which may contain more media items."""

    object_id: str
    parent_id: str | None
    title: str


@dataclass(frozen=True, slots=True)
class BrowsePage:
    """One paginated UPnP ``Browse`` response."""

    containers: tuple[MediaContainer, ...]
    items: tuple[MediaItem, ...]
    number_returned: int
    total_matches: int


def _local_name(tag: str) -> str:
    """Return an XML local name regardless of namespace prefix."""

    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def _first_text(element: ET.Element, local_name: str) -> str | None:
    """Find the first descendant with ``local_name`` and return stripped text."""

    for descendant in element.iter():
        if _local_name(descendant.tag) == local_name:
            value = "".join(descendant.itertext()).strip()
            return value or None
    return None


def _parse_int(value: str | None) -> int | None:
    """Parse a non-negative integer, returning ``None`` for bad metadata."""

    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")


def _parse_resolution(value: str | None) -> tuple[int, int] | None:
    """Convert a UPnP ``WIDTHxHEIGHT`` value into an integer tuple."""

    if not value:
        return None
    match = _RESOLUTION_RE.match(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _soap_envelope(action: str, service: str, arguments: Sequence[tuple[str, str]]) -> bytes:
    """Build the simple SOAP 1.1 envelopes accepted by Sony cameras."""

    arguments_xml = "".join(
        f"<{name}>{xml_escape(value)}</{name}>" for name, value in arguments
    )
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_ENV_NS}" '
        f's:encodingStyle="{SOAP_ENV_NS}encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service}">{arguments_xml}</u:{action}>'
        "</s:Body>"
        "</s:Envelope>"
    )
    return payload.encode("utf-8")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Read a response header from either HTTPMessage or a plain test mapping."""

    direct = headers.get(name)
    if direct is not None:
        return direct
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def _response_status(response: _HttpResponse) -> int:
    """Get a status code from urllib or an injected compatible response."""

    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()  # type: ignore[attr-defined, no-untyped-call]
    return int(status or 0)


def _read_limited(response: _HttpResponse, limit: int = MAX_XML_BYTES) -> bytes:
    """Read a bounded XML response to avoid unbounded memory consumption."""

    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ProtocolError(f"camera XML response exceeds {limit} bytes")
    return payload


def _extract_soap_fault(payload: bytes) -> SoapFault | None:
    """Parse a SOAP Fault if ``payload`` contains one."""

    if not payload.strip():
        return None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    for element in root.iter():
        if _local_name(element.tag) == "Fault":
            return SoapFault(
                _first_text(element, "faultcode"),
                _first_text(element, "faultstring") or _first_text(element, "errorDescription"),
            )
    return None


def _safe_filename(title: str) -> str:
    """Convert an untrusted camera title into one Windows-safe file name."""

    leaf = title.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = "".join(character for character in leaf if ord(character) >= 32)
    leaf = re.sub(r'[<>:"/\\|?*]', "_", leaf).strip(" .")
    if not leaf:
        leaf = "image"

    stem, suffix = os.path.splitext(leaf)
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    if stem.upper() in reserved:
        stem = f"_{stem}"

    # Leave enough room for the destination path and the temporary .part suffix.
    maximum = 180
    if len(stem) + len(suffix) > maximum:
        stem = stem[: max(1, maximum - len(suffix))]
    return f"{stem}{suffix}"


class SonySoapClient:
    """Client for a Sony camera in ``Send to Smartphone`` mode.

    Parameters
    ----------
    host:
        Camera address.  Sony cameras normally use ``192.168.122.1`` in this
        direct Wi-Fi mode.
    port:
        Camera HTTP port, normally ``64321``.
    timeout:
        Per-request timeout in seconds.
    opener:
        Optional ``urllib.request.urlopen`` compatible callable.  Injection is
        useful for deterministic tests and applications with custom networking.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 10.0,
        opener: UrlOpen | None = None,
        scheme: str = "http",
    ) -> None:
        if not host or "://" in host or any(character in host for character in "/?#"):
            raise ValueError("host must be a host name or IP address, not a URL")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if scheme not in {"http", "https"}:
            raise ValueError("scheme must be http or https")

        self.host = host
        self.port = port
        self.timeout = timeout
        self.base_url = f"{scheme}://{host}:{port}"
        self._urlopen: UrlOpen = opener or urlopen
        self._xpush_control_url = self.base_url + DEFAULT_XPUSH_CONTROL_PATH
        self._content_control_url = self.base_url + DEFAULT_CONTENT_CONTROL_PATH
        self._transfer_started = False

    @classmethod
    def discover(
        cls,
        hosts: Iterable[str] = (DEFAULT_HOST,),
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 1.0,
        opener: UrlOpen | None = None,
    ) -> list[CameraDescriptor]:
        """Probe candidate hosts and return descriptors for responding cameras.

        The function does not scan a subnet.  In direct-camera mode the current
        Wi-Fi default gateway, followed by ``192.168.122.1``, are suitable
        candidates for the caller to provide.
        """

        found: list[CameraDescriptor] = []
        for host in hosts:
            client = cls(host, port, timeout=timeout, opener=opener)
            try:
                found.append(client.probe())
            except SonySoapError:
                continue
        return found

    def probe(self) -> CameraDescriptor:
        """Fetch and validate ``DmsDescPush.xml`` and resolve control URLs."""

        descriptor_url = self.base_url + DESCRIPTOR_PATH
        payload = self._request_bytes("GET", descriptor_url)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ProtocolError("DmsDescPush.xml is not valid XML") from exc

        manufacturer = _first_text(root, "manufacturer")
        if manufacturer and "sony" not in manufacturer.casefold():
            raise ProtocolError(f"descriptor manufacturer is not Sony: {manufacturer!r}")

        xpush_url: str | None = None
        content_url: str | None = None
        for service in root.iter():
            if _local_name(service.tag) != "service":
                continue
            service_type = _first_text(service, "serviceType") or ""
            control_path = _first_text(service, "controlURL")
            if not control_path:
                continue
            if "XPushList" in service_type:
                xpush_url = self._camera_url(control_path)
            elif "ContentDirectory" in service_type:
                content_url = self._camera_url(control_path)

        self._xpush_control_url = xpush_url or (
            self.base_url + DEFAULT_XPUSH_CONTROL_PATH
        )
        self._content_control_url = content_url or (
            self.base_url + DEFAULT_CONTENT_CONTROL_PATH
        )
        return CameraDescriptor(
            descriptor_url=descriptor_url,
            manufacturer=manufacturer,
            model_name=_first_text(root, "modelName"),
            friendly_name=_first_text(root, "friendlyName"),
            xpush_control_url=self._xpush_control_url,
            content_directory_control_url=self._content_control_url,
        )

    def start_transfer(self) -> None:
        """Invoke Sony ``X_TransferStart`` so the camera enters transfer state."""

        if self._transfer_started:
            return
        self._soap_request(
            self._xpush_control_url,
            XPUSH_SERVICE,
            "X_TransferStart",
            (),
        )
        self._transfer_started = True

    def end_transfer(self, error_code: int = 0) -> None:
        """Invoke Sony ``X_TransferEnd`` and leave transfer mode cleanly."""

        self._soap_request(
            self._xpush_control_url,
            XPUSH_SERVICE,
            "X_TransferEnd",
            (("ErrCode", str(error_code)),),
        )
        self._transfer_started = False

    # Explicit aliases mirror Sony's action naming while keeping Python verbs.
    transfer_start = start_transfer
    transfer_end = end_transfer

    @contextmanager
    def transfer_session(self) -> Iterator["SonySoapClient"]:
        """Start a transfer and always request a clean end on context exit."""

        self.start_transfer()
        try:
            yield self
        finally:
            self.end_transfer()

    def browse_page(
        self,
        object_id: str,
        *,
        starting_index: int = 0,
        requested_count: int = 9999,
    ) -> BrowsePage:
        """Return one page of direct children for a UPnP object/container."""

        if starting_index < 0:
            raise ValueError("starting_index must not be negative")
        if requested_count <= 0:
            raise ValueError("requested_count must be positive")

        payload = self._soap_request(
            self._content_control_url,
            CONTENT_DIRECTORY_SERVICE,
            "Browse",
            (
                ("ObjectID", object_id),
                ("BrowseFlag", "BrowseDirectChildren"),
                ("Filter", "*"),
                ("StartingIndex", str(starting_index)),
                ("RequestedCount", str(requested_count)),
                ("SortCriteria", ""),
            ),
        )
        return self._parse_browse_response(payload)

    def browse_all(self, root_id: str | None = None) -> list[MediaItem]:
        """Recursively enumerate all media below ``PushRoot`` or ``PhotoRoot``.

        With no explicit root, ``PushRoot`` is attempted first.  Sony uses it
        when images were selected on the camera.  If that root is rejected,
        ``PhotoRoot`` is used for the camera-side "select on smartphone" view.
        The transfer is started automatically and remains active for subsequent
        downloads until ``end_transfer()`` is called.
        """

        self.start_transfer()
        if root_id is not None:
            return self._browse_tree(root_id)
        try:
            return self._browse_tree("PushRoot")
        except (HttpStatusError, SoapFault, ProtocolError):
            return self._browse_tree("PhotoRoot")

    def download(
        self,
        item: MediaItem,
        destination: str | os.PathLike[str],
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        """Download ``item`` and atomically return its final path.

        ``destination`` may be an existing output directory or an explicit file
        path (the latter is recognized by its suffix).  Data is written to
        ``<filename>.part``.  Only after the advertised byte length is satisfied
        is the file flushed, synced, and atomically renamed.  An interrupted or
        short response leaves the ``.part`` file for diagnostics while never
        exposing a corrupt final photograph.
        """

        resource = item.best_resource()
        resource_url = self._camera_url(resource.url)
        destination_path = Path(destination)
        if destination_path.exists() and destination_path.is_dir():
            output_directory = destination_path
            final_path = output_directory / _safe_filename(item.title)
        elif not destination_path.exists() and not destination_path.suffix:
            output_directory = destination_path
            final_path = output_directory / _safe_filename(item.title)
        else:
            output_directory = destination_path.parent
            final_path = output_directory / _safe_filename(destination_path.name)
        output_directory.mkdir(parents=True, exist_ok=True)

        if final_path.exists():
            if resource.size is not None and final_path.stat().st_size == resource.size:
                return final_path
            final_path = self._unused_destination(final_path)
        part_path = final_path.with_name(final_path.name + ".part")

        request = Request(resource_url, method="GET")
        response = self._open(request)
        with closing(response):
            status = _response_status(response)
            if not 200 <= status < 300:
                body = _read_limited(response)
                fault = _extract_soap_fault(body)
                if fault:
                    raise fault
                raise HttpStatusError(status, resource_url)

            content_length = _parse_int(_header_value(response.headers, "Content-Length"))
            expected = content_length if content_length is not None else resource.size
            written = 0
            try:
                with part_path.open("wb") as output:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
                        if progress_cb is not None:
                            progress_cb(written, expected)
                    output.flush()
                    os.fsync(output.fileno())
            except OSError as exc:
                raise DownloadError(f"could not write {part_path}") from exc

        if expected is not None and written != expected:
            raise DownloadError(
                f"incomplete download for {item.title!r}: received {written}, expected {expected}"
            )

        # Recheck after the network operation so a concurrent download never
        # overwrites an independently completed file.
        if final_path.exists():
            final_path = self._unused_destination(final_path)
        try:
            os.replace(part_path, final_path)
        except OSError as exc:
            raise DownloadError(f"could not commit {final_path}") from exc
        return final_path

    def _browse_tree(self, root_id: str) -> list[MediaItem]:
        """Walk a container tree with pagination and cycle protection."""

        queue = [root_id]
        visited_containers: set[str] = set()
        seen_items: set[tuple[str, str]] = set()
        result: list[MediaItem] = []

        while queue:
            object_id = queue.pop(0)
            if object_id in visited_containers:
                continue
            visited_containers.add(object_id)
            starting_index = 0

            while True:
                page = self.browse_page(object_id, starting_index=starting_index)
                for container in page.containers:
                    if container.object_id not in visited_containers:
                        queue.append(container.object_id)
                for item in page.items:
                    key = (item.object_id, item.title)
                    if key not in seen_items:
                        seen_items.add(key)
                        result.append(item)

                if starting_index + page.number_returned >= page.total_matches:
                    break
                if page.number_returned <= 0:
                    raise ProtocolError(
                        f"camera pagination stalled for object {object_id!r}"
                    )
                starting_index += page.number_returned

        return result

    def _parse_browse_response(self, payload: bytes) -> BrowsePage:
        """Decode the outer SOAP response and its escaped DIDL-Lite document."""

        fault = _extract_soap_fault(payload)
        if fault:
            raise fault
        try:
            outer = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ProtocolError("Browse response is not valid XML") from exc

        result_element: ET.Element | None = None
        for element in outer.iter():
            if _local_name(element.tag) == "Result":
                result_element = element
                break
        if result_element is None:
            raise ProtocolError("Browse response has no Result element")

        if list(result_element):
            didl_payload = b"".join(
                ET.tostring(child, encoding="utf-8") for child in result_element
            )
        else:
            didl_payload = (result_element.text or "").strip().encode("utf-8")

        containers: list[MediaContainer] = []
        items: list[MediaItem] = []
        if didl_payload:
            try:
                didl = ET.fromstring(didl_payload)
            except ET.ParseError as exc:
                raise ProtocolError("Browse Result does not contain valid DIDL-Lite XML") from exc

            for element in didl:
                kind = _local_name(element.tag)
                object_id = element.attrib.get("id", "")
                parent_id = element.attrib.get("parentID")
                title = _first_text(element, "title") or object_id or "image"
                if kind == "container":
                    if object_id:
                        containers.append(MediaContainer(object_id, parent_id, title))
                    continue
                if kind != "item":
                    continue

                resources: list[MediaResource] = []
                for resource_element in element.iter():
                    if _local_name(resource_element.tag) != "res":
                        continue
                    url = (resource_element.text or "").strip()
                    if not url:
                        continue
                    resources.append(
                        MediaResource(
                            url=url,
                            size=_parse_int(resource_element.attrib.get("size")),
                            resolution=_parse_resolution(
                                resource_element.attrib.get("resolution")
                            ),
                            protocol_info=resource_element.attrib.get("protocolInfo", ""),
                        )
                    )
                items.append(MediaItem(object_id, parent_id, title, tuple(resources)))

        number_returned = _parse_int(_first_text(outer, "NumberReturned"))
        total_matches = _parse_int(_first_text(outer, "TotalMatches"))
        actual_count = len(containers) + len(items)
        if number_returned is None:
            number_returned = actual_count
        if total_matches is None:
            total_matches = number_returned
        if total_matches < number_returned:
            raise ProtocolError("Browse TotalMatches is smaller than NumberReturned")
        return BrowsePage(
            tuple(containers), tuple(items), number_returned, total_matches
        )

    def _soap_request(
        self,
        url: str,
        service: str,
        action: str,
        arguments: Sequence[tuple[str, str]],
    ) -> bytes:
        """Send one SOAP action and raise a typed exception for SOAP faults."""

        payload = _soap_envelope(action, service, arguments)
        response_payload = self._request_bytes(
            "POST",
            url,
            data=payload,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{service}#{action}"',
            },
        )
        fault = _extract_soap_fault(response_payload)
        if fault:
            raise fault
        return response_payload

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """Perform a bounded HTTP request and return its response body."""

        request = Request(url, data=data, headers=dict(headers or {}), method=method)
        response = self._open(request)
        with closing(response):
            status = _response_status(response)
            payload = _read_limited(response)
            if not 200 <= status < 300:
                fault = _extract_soap_fault(payload)
                if fault:
                    raise fault
                raise HttpStatusError(status, url)
            return payload

    def _open(self, request: Request) -> _HttpResponse:
        """Call the injected opener and normalize urllib transport failures."""

        try:
            return self._urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            try:
                payload = exc.read(MAX_XML_BYTES + 1)
            finally:
                exc.close()
            fault = _extract_soap_fault(payload)
            if fault:
                raise fault from exc
            raise HttpStatusError(exc.code, request.full_url) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"could not reach camera at {request.full_url}: {exc}") from exc

    def _camera_url(self, value: str) -> str:
        """Resolve a URL and require it to remain on the configured camera."""

        resolved = urljoin(self.base_url + "/", value)
        base = urlsplit(self.base_url)
        target = urlsplit(resolved)
        if target.scheme not in {"http", "https"}:
            raise UnsafeResourceUrlError(f"unsupported camera URL scheme: {resolved}")

        def origin(parts: object) -> tuple[str | None, int | None]:
            split = parts  # keep mypy/pyright independent of a concrete parser class
            hostname = split.hostname.casefold() if split.hostname else None  # type: ignore[attr-defined]
            try:
                port = split.port  # type: ignore[attr-defined]
            except ValueError as exc:
                raise UnsafeResourceUrlError(f"invalid port in camera URL: {resolved}") from exc
            if port is None:
                port = 443 if split.scheme == "https" else 80  # type: ignore[attr-defined]
            return hostname, port

        if target.scheme != base.scheme or origin(target) != origin(base):
            raise UnsafeResourceUrlError(
                f"camera resource points outside {self.base_url}: {resolved}"
            )
        return resolved

    @staticmethod
    def _unused_destination(path: Path) -> Path:
        """Find a non-existing sibling without overwriting a prior photograph."""

        for number in range(1, 10000):
            candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
            if not candidate.exists() and not candidate.with_name(
                candidate.name + ".part"
            ).exists():
                return candidate
        raise DownloadError(f"could not allocate a unique destination for {path.name}")


__all__ = [
    "BrowsePage",
    "CameraDescriptor",
    "DownloadError",
    "HttpStatusError",
    "MediaContainer",
    "MediaItem",
    "MediaResource",
    "NoResourceError",
    "ProtocolError",
    "SoapFault",
    "SonySoapClient",
    "SonySoapError",
    "TransportError",
    "UnsafeResourceUrlError",
]
