"""Calls from the Android activity into the shared camera session.

All calls come from one background thread of the activity, so no locking
is needed here beyond what ``CameraSession`` already does.
"""

import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor

import os
import time

import airmtp
from camera_session import CameraSession, SessionError
import sony_soap
from sony_soap import SonySoapClient, SonySoapError

PTPIP_PORT = 15740
VIDEO_SUFFIXES = (".MP4", ".MOV", ".MTS", ".M2TS", ".AVI")

_session = None  # "Send to Computer" (PTP/IP) session
_files = []
_direct = None  # "Send to Smartphone" (camera's own Wi-Fi) client
_items = []


def _port_open(address, timeout):
    try:
        with socket.create_connection((address, PTPIP_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _scan_subnet(own_address):
    """Find a camera listening for PTP/IP in this interface's /24.

    Used when SSDP discovery gets no answer, which can happen on a phone's
    hotspot; the camera there gets a random address from the phone.
    """

    prefix = own_address.rsplit(".", 1)[0]
    candidates = ["%s.%d" % (prefix, n) for n in range(1, 255) if "%s.%d" % (prefix, n) != own_address]
    with ThreadPoolExecutor(max_workers=64) as pool:
        for address, is_open in zip(candidates, pool.map(lambda a: _port_open(a, 0.6), candidates)):
            if is_open:
                return address
    return None


def _try(work_dir, ip_address, interface, attempts):
    session = CameraSession(
        ip_address=ip_address,
        multicast_interface=interface,
        hostname="camera-transfer",
        work_dir=work_dir,
        discovery_attempts=int(attempts),
    )
    session.connect()
    return session


def connect(work_dir, ip_address, interfaces_csv, attempts_per_interface, last_ip=""):
    """Find the camera and list its files; returns JSON for the activity.

    Order: an address typed by the user; the camera's last address if it
    still answers; SSDP discovery on each local interface (Wi-Fi first,
    then the hotspot); finally a scan of each interface's subnet.
    """

    global _session, _files
    close()
    ip_address = (ip_address or "").strip()
    interfaces = [i for i in (interfaces_csv or "").split(",") if i]
    errors = []
    session = None
    if ip_address:
        try:
            session = _try(work_dir, ip_address, None, attempts_per_interface)
        except SessionError as exc:
            errors.append("%s: %s" % (ip_address, exc))
    else:
        if last_ip and _port_open(last_ip, 0.8):
            try:
                session = _try(work_dir, last_ip, None, attempts_per_interface)
            except SessionError as exc:
                errors.append("%s: %s" % (last_ip, exc))
        for interface in ([] if session else interfaces or [None]):
            try:
                session = _try(work_dir, "auto", interface, attempts_per_interface)
                break
            except SessionError as exc:
                errors.append("ssdp %s: %s" % (interface, exc))
        for interface in ([] if session else interfaces):
            found = _scan_subnet(interface)
            if not found:
                errors.append("scan %s: nothing on port %d" % (interface, PTPIP_PORT))
                continue
            try:
                session = _try(work_dir, found, None, attempts_per_interface)
                break
            except SessionError as exc:
                errors.append("%s: %s" % (found, exc))
    if session is None:
        raise SessionError("; ".join(errors) or "camera not found")
    _session = session
    _files = session.list_files()
    capacity, free, label = session.storage()
    try:
        camera_ip = airmtp.g.socketPrimary.getpeername()[0]
    except Exception:  # noqa: BLE001
        camera_ip = ip_address or last_ip or ""
    return json.dumps({
        "ip": camera_ip,
        "model": session.camera_model,
        "capacity": capacity,
        "free": free,
        "label": label,
        "files": [
            {
                "name": f.name,
                "size": f.size,
                "captured": f.captured,
                "raw": f.is_raw,
                "video": f.is_video,
            }
            for f in _files
        ],
    })


_diag_path = None


def _diag(*parts):
    """Log to logcat and to a file that can be pulled with adb."""

    line = " ".join(str(p) for p in parts)
    print("camera-direct:", line)
    if _diag_path:
        try:
            with open(_diag_path, "a", encoding="utf-8") as handle:
                handle.write(time.strftime("%H:%M:%S ") + line + "\n")
        except OSError:
            pass


def connect_direct(wait_seconds, diag_path=""):
    """Connect to a camera in "Send to Smartphone" mode on its own Wi-Fi.

    The activity has already joined the camera's network and bound the app
    to it; the camera's web server can need a few seconds to answer.
    """

    global _direct, _items, _diag_path
    close()
    _diag_path = diag_path or None
    if _diag_path:
        try:
            os.remove(_diag_path)
        except OSError:
            pass
    client = SonySoapClient(timeout=10.0)
    deadline = time.time() + float(wait_seconds)
    while True:
        try:
            descriptor = client.probe()
            break
        except SonySoapError as error:
            _diag("probe not answered yet:", error)
            if time.time() > deadline:
                raise
            time.sleep(1.0)
    _diag("camera:", descriptor.manufacturer, descriptor.model_name, descriptor.friendly_name)
    # Order recorded from Sony's own app (socket log): descriptor on 64321,
    # then the camera's device-info files on 60151, and only then the SOAP
    # calls.  Asking for the list first makes the camera answer 506.
    _fetch_device_info(client.host)

    # "Send this image" mode: the camera offers the picture it is showing in
    # DigitalImagingDesc.xml (X_CurrentContent_URL).  The catalogue and
    # X_TransferStart are not available then (they answer UPnP 506).
    current = _read_current_content(client)
    if current:
        _direct = client
        _items = current
        return _direct_listing(client, descriptor)

    items = None
    failures = []
    try:
        client.start_transfer()
        _diag("X_TransferStart ok")
    except SonySoapError as error:
        _diag("X_TransferStart rejected:", error)
        failures.append(f"TransferStart: {error}")
    roots = []
    try:
        reply = client._soap_request(client._xpush_control_url, sony_soap.XPUSH_SERVICE, "X_GetPushRoot", ())
        found = re.search(rb"<ObjectID>([^<]*)</ObjectID>", reply)
        if found:
            roots.append(found.group(1).decode())
        _diag("X_GetPushRoot ->", roots)
    except SonySoapError as error:
        _diag("X_GetPushRoot failed:", error)
        failures.append(f"GetPushRoot: {error}")
    for browse_root in roots + ["PushRoot", "PhotoRoot"]:
        try:
            items = client._browse_tree(browse_root)
            _diag(f"browse {browse_root}: {len(items)} items")
            if items:
                break
        except SonySoapError as error:
            _diag(f"browse {browse_root} failed:", error)
            failures.append(f"{browse_root}: {error}")
    if not items:
        _probe_more(client)
        _run_probe_script(client)
        items = _retry_browse(client, 15)
    if not items:
        # Maybe the camera offers the pictures only after the selection on its
        # screen is confirmed: keep asking for a while.
        items = _wait_for_selection(client, 60)
    if not items:
        raise SonySoapError("; ".join(dict.fromkeys(failures)) or "the camera lists no pictures")
    _direct = client
    _items = items
    return _direct_listing(client, descriptor)


def _wait_for_selection(client, seconds):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        current = _read_current_content(client, quiet=True)
        if current:
            _diag("waiting: current content appeared")
            return current
        try:
            items = client._browse_tree("PushRoot")
            if items:
                _diag("waiting: PushRoot has", len(items), "items")
                return items
            state = "PushRoot empty"
        except SonySoapError as error:
            state = str(error)
        if state != last:
            _diag("waiting:", state)
            last = state
        time.sleep(2.0)
    _diag("waiting for a selection gave up after", seconds, "s")
    return None


def _direct_listing(client, descriptor):
    return json.dumps({
        "ip": client.host,
        "model": descriptor.model_name or descriptor.friendly_name or "Sony",
        "capacity": 0,
        "free": 0,
        "label": "",
        "direct": True,
        "files": [
            {
                "name": item.title,
                "size": item.size or 0,
                "captured": "",
                "raw": False,
                "video": item.title.upper().endswith(VIDEO_SUFFIXES),
            }
            for item in _items
        ],
    })


_SONY_HEADERS = {
    "User-Agent": "UPnP/1.0 DLNADOC/1.50",
    "X-AV-Client-Info": 'av=5.0; hn=""; cn="Sony Corp."; mn="PMlib"; mv="2.8.1";',
    "Connection": "close",
}
_PROFILE_ORDER = ("JPEG_LRG", "JPEG_SM", "JPEG_TN")  # best first


class _CurrentContent:
    """The one picture a camera in "send this image" mode offers.

    The camera names it after the original (DSC04117.ARW) but serves JPEG
    renditions of it (thumbnail, small, large); the original RAW is not
    offered in this mode.
    """

    def __init__(self, stem, urls):
        self.title = stem + ".JPG"
        self.urls = urls  # profile id -> url
        self.size = 0

    def best_url(self):
        for profile in _PROFILE_ORDER:
            if profile in self.urls:
                return self.urls[profile]
        return next(iter(self.urls.values()))


def _read_current_content(client, quiet=False):
    import xml.etree.ElementTree as ET
    import urllib.request

    try:
        raw = client._request_bytes("GET", client.base_url + "/DigitalImagingDesc.xml")
        root = ET.fromstring(raw)
    except Exception as error:
        _diag("DigitalImagingDesc.xml unreadable:", error)
        return []
    # One picture is offered as several renditions (TN_, SM_, LRG_ + the same
    # name); group by the name so several chosen pictures stay separate.
    by_stem = {}
    for element in root.iter():
        if sony_soap._local_name(element.tag) != "X_CurrentContent_URL_URL":
            continue
        url = (element.text or "").strip()
        if not url:
            continue
        name = url.split("?")[0].rsplit("/", 1)[-1]  # LRG_DSC04117.ARW
        name = re.sub(r"^(TN|SM|LRG)_", "", name)
        stem = name.rsplit(".", 1)[0]
        by_stem.setdefault(stem, {})[element.attrib.get("profileID", "")] = url
    if not by_stem:
        if not quiet:
            _diag("no current content offered")
        return []
    items = []
    for stem, urls in by_stem.items():
        item = _CurrentContent(stem, urls)
        # Size for the progress bar: ask the camera for the headers only.
        for method in ("HEAD", "GET"):
            try:
                request = urllib.request.Request(item.best_url(), headers=_SONY_HEADERS, method=method)
                with urllib.request.urlopen(request, timeout=15) as response:
                    item.size = int(response.headers.get("Content-Length") or 0)
                break
            except Exception as error:
                _diag("size", method, "failed:", error)
        _diag("current content:", item.title, sorted(urls), "size", item.size)
        items.append(item)
    return items


def _run_probe_script(client):
    """Developer aid while the camera protocol is being worked out: run
    probe.py from the app's external files folder (next to camera-direct.log,
    pushed with adb) against the connected camera.  Only the developer can
    put a file there."""

    if not _diag_path:
        return
    script = os.path.join(os.path.dirname(_diag_path), "probe.py")
    if not os.path.exists(script):
        return
    try:
        with open(script, encoding="utf-8") as handle:
            code = handle.read()
        exec(compile(code, script, "exec"), {"client": client, "diag": _diag, "host": client.host})
    except Exception as error:  # diagnostics only
        _diag("probe.py failed:", repr(error))


def _fetch_device_info(host):
    """Download the camera's device-info files completely, as Sony's app does."""

    import urllib.request

    headers = {
        "User-Agent": "UPnP/1.0 DLNADOC/1.50",
        "X-AV-Client-Info": 'av=5.0; hn=""; cn="Sony Corp."; mn="PMlib"; mv="2.8.1";',
        "Connection": "close",
    }
    for path in ("/deviceInfo/deviceNumber.bin", "/deviceInfo/deviceFile.bin"):
        started = time.time()
        try:
            request = urllib.request.Request(f"http://{host}:60151{path}", headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                size = len(response.read())
            _diag("device info", path, size, "bytes in", round(time.time() - started, 1), "s")
        except Exception as error:
            _diag("device info", path, "failed:", error)


def _probe_ports(host):
    """Which TCP ports does the camera answer on in this mode?"""

    open_ports = []
    for port in (80, 443, 8080, 8000, 10000, 15740, 5353, 5002, 55001, 60151, 64321):
        try:
            with socket.create_connection((host, port), timeout=1.5):
                open_ports.append(port)
        except OSError:
            pass
    _diag("open ports on", host, ":", open_ports)


def _probe_handshake(host):
    """The camera's screen says "Connecting": something may be expected from
    the phone on the second port (60151, device info) or over SSDP."""

    import urllib.request

    for path in ("/", "/deviceInfo/deviceNumber.bin", "/deviceInfo/deviceFile.bin",
                 "/deviceInfo/", "/DmsDescPush.xml"):
        url = f"http://{host}:60151{path}"
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                body = response.read(400)
                _diag("GET", url, "->", response.status, dict(response.headers), "len", len(body),
                      body[:48].hex() if path.endswith(".bin") else body[:300].decode("utf-8", "replace"))
        except Exception as error:
            detail = ""
            if hasattr(error, "read"):
                try:
                    detail = error.read(200).decode("utf-8", "replace")
                except Exception:
                    pass
            _diag("GET", url, "failed:", error, detail)

    # Unicast SSDP search straight at the camera (needs no multicast lock).
    for target in ("ssdp:all", "upnp:rootdevice", "urn:schemas-sony-com:service:XPushList:1"):
        message = (
            "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
            f"MX: 2\r\nST: {target}\r\n\r\n"
        ).encode()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.5)
            sock.sendto(message, (host, 1900))
            data, _ = sock.recvfrom(2048)
            _diag("SSDP", target, "->", data.decode("utf-8", "replace").replace("\r\n", " | ")[:400])
        except Exception as error:
            _diag("SSDP", target, "no answer:", error)
        finally:
            sock.close()


def _retry_browse(client, seconds=60):
    """The camera might answer 506 only while it is still busy (building its
    picture list, waiting for a menu step); keep asking and log any change."""

    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        for use_push in (False, True):
            if use_push:
                try:
                    client.start_transfer()
                except SonySoapError:
                    pass
            for browse_root in ("PhotoRoot", "PushRoot"):
                try:
                    items = client._browse_tree(browse_root)
                    _diag(f"retry: browse {browse_root} worked: {len(items)} items")
                    if items:
                        return items
                    state = f"{browse_root}: empty"
                except SonySoapError as error:
                    state = f"{browse_root}: {error}"
                if state != last:
                    _diag("retry state:", state)
                    last = state
        time.sleep(3.0)
    _diag("retry gave up after", seconds, "s")
    return None


def _probe_variants(client, url):
    """Send GetSystemUpdateID in several wire formats: every action fails
    with UPnP 506, so the request format itself may be what the camera dislikes."""

    import urllib.request
    import sony_soap

    service = sony_soap.CONTENT_DIRECTORY_SERVICE
    ns = "http://schemas.xmlsoap.org/soap/envelope/"
    full = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<s:Envelope xmlns:s="{ns}" s:encodingStyle="{ns}encoding/"><s:Body>'
        f'<u:GetSystemUpdateID xmlns:u="{service}"></u:GetSystemUpdateID></s:Body></s:Envelope>'
    ).encode()
    plain = full.replace(f' s:encodingStyle="{ns}encoding/"'.encode(), b"")
    selfclosed = full.replace(b"></u:GetSystemUpdateID>", b"/>")
    action = f'"{service}#GetSystemUpdateID"'
    ctype = 'text/xml; charset="utf-8"'
    upnp_ua = "Linux/5.0 UPnP/1.0 CameraTransfer/1.0"
    variants = [
        ("baseline", full, {"Content-Type": ctype, "SOAPACTION": action}),
        ("upnp user-agent", full, {"Content-Type": ctype, "SOAPACTION": action, "User-Agent": upnp_ua}),
        ("unquoted action", full, {"Content-Type": ctype, "SOAPACTION": action.strip('"')}),
        ("charset=utf-8 plain", full, {"Content-Type": "text/xml; charset=utf-8", "SOAPACTION": action}),
        ("no encodingStyle", plain, {"Content-Type": ctype, "SOAPACTION": action}),
        ("self-closed action", selfclosed, {"Content-Type": ctype, "SOAPACTION": action}),
        ("keep-alive + ua", full, {"Content-Type": ctype, "SOAPACTION": action, "User-Agent": upnp_ua,
                                   "Connection": "keep-alive", "Accept": "*/*"}),
    ]
    for name, body, headers in variants:
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=8) as response:
                _diag("variant", name, "-> HTTP", response.status, response.read(300).decode("utf-8", "replace"))
        except Exception as error:
            detail = ""
            if hasattr(error, "read"):
                try:
                    detail = error.read(300).decode("utf-8", "replace")
                except Exception:
                    pass
            _diag("variant", name, "failed:", error, detail.replace("\\n", " "))


def _probe_more(client):
    """Extra read-only queries, logged to find what state the camera wants."""

    import sony_soap

    def call(label, url, service, action, args=()):
        try:
            reply = client._soap_request(url, service, action, args)
            _diag(label, "->", reply.decode("utf-8", "replace").replace("\\n", " ")[:700])
        except Exception as error:  # diagnostics only
            _diag(label, "failed:", error)

    push = client._xpush_control_url
    content = client._content_control_url
    _probe_variants(client, content)
    call("X_GetPushRoot", push, sony_soap.XPUSH_SERVICE, "X_GetPushRoot")
    call("GetSystemUpdateID", content, sony_soap.CONTENT_DIRECTORY_SERVICE, "GetSystemUpdateID")
    for object_id in ("0", "PhotoRoot", "PushRoot"):
        call(
            f"BrowseMetadata {object_id}", content, sony_soap.CONTENT_DIRECTORY_SERVICE, "Browse",
            (("ObjectID", object_id), ("BrowseFlag", "BrowseMetadata"), ("Filter", "*"),
             ("StartingIndex", "0"), ("RequestedCount", "1"), ("SortCriteria", "")),
        )
    try:
        raw = client._request_bytes("GET", client.base_url + "/DigitalImagingDesc.xml")
        _diag("DigitalImagingDesc.xml:", len(raw), "bytes:", raw.decode("utf-8", "replace").replace("\\n", " ")[:8000])
    except Exception as error:
        _diag("DigitalImagingDesc.xml failed:", error)


def _log_services(client):
    """Log the camera's services and their actions (nothing secret in them)."""

    import xml.etree.ElementTree as ET
    from sony_soap import _first_text, _local_name

    for path in ("/DmsDescPush.xml", "/DmsDesc.xml"):
        try:
            raw = client._request_bytes("GET", client.base_url + path)
        except Exception as error:  # diagnostics only
            _diag(path, "not available:", error)
            continue
        _diag(path, "ok,", len(raw), "bytes")
        try:
            doc_root = ET.fromstring(raw)
        except ET.ParseError as error:
            _diag(path, "not XML:", error)
            continue
        for service in doc_root.iter():
            if _local_name(service.tag) != "service":
                continue
            kind = _first_text(service, "serviceType")
            scpd = _first_text(service, "SCPDURL")
            _diag(" service", kind, "control", _first_text(service, "controlURL"), "scpd", scpd)
            if not scpd:
                continue
            try:
                doc = ET.fromstring(client._request_bytes("GET", client._camera_url(scpd)))
            except Exception as error:
                _diag("  scpd failed:", error)
                continue
            for action in doc.iter():
                if _local_name(action.tag) != "action":
                    continue
                args = [
                    f"{_first_text(arg, 'direction')}:{_first_text(arg, 'name')}"
                    for arg in action.iter() if _local_name(arg.tag) == "argument"
                ]
                _diag("  action", _first_text(action, "name"), args)


def _smallest_resource(item):
    marked = [r for r in item.resources if "_TN" in r.protocol_info.upper()]
    pool = marked or list(item.resources)
    return min(pool, key=lambda r: (r.size or 1 << 40, r.pixel_count or 1 << 40))


def _fetch(url):
    import urllib.request

    request = urllib.request.Request(url, headers=_SONY_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def thumbnail(index):
    if _direct is not None:
        item = _items[int(index)]
        if isinstance(item, _CurrentContent):
            return _fetch(item.urls.get("JPEG_TN") or item.best_url())
        resource = _smallest_resource(item)
        return _direct._request_bytes("GET", _direct._camera_url(resource.url))
    return _require().thumbnail(_files[int(index)])


def download(index, target, listener):
    """Copy file ``index`` to ``target``; returns the path actually written.

    ``listener.onProgress(done, total)`` reports progress.
    """

    if _direct is not None:
        item = _items[int(index)]
        if os.path.exists(target):
            os.remove(target)
        expected = item.size or 0
        if isinstance(item, _CurrentContent):
            return _download_url(item.best_url(), target, expected, listener)
        path = _direct.download(
            item, target, progress_cb=lambda done, total=None: listener.onProgress(done, total or expected)
        )
        return str(path)
    item = _files[int(index)]
    return _require().download(item, target, lambda done, total: listener.onProgress(done, total))


def _download_url(url, target, expected, listener):
    import urllib.request

    request = urllib.request.Request(url, headers=_SONY_HEADERS)
    part = target + ".part"
    done = 0
    with urllib.request.urlopen(request, timeout=30) as response:
        total = int(response.headers.get("Content-Length") or expected or 0)
        with open(part, "wb") as handle:
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                handle.write(block)
                done += len(block)
                listener.onProgress(done, total or done)
    if total and done != total:
        os.remove(part)
        raise SonySoapError(f"download cut short: {done} of {total} bytes")
    os.replace(part, target)
    return target


def is_connected():
    if _direct is not None:
        return True
    return _session is not None and _session.connected


def close():
    global _session, _files, _direct, _items
    session, _session, _files = _session, None, []
    direct, _direct, _items = _direct, None, []
    if session is not None:
        session.close(sleep_camera=True)
    if direct is not None:
        try:
            direct.end_transfer(0)
        except SonySoapError:
            pass


def _require():
    if _session is None:
        raise SessionError("not connected")
    return _session
