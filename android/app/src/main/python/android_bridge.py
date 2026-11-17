"""Calls from the Android activity into the shared camera session.

All calls come from one background thread of the activity, so no locking
is needed here beyond what ``CameraSession`` already does.
"""

import json
import socket
from concurrent.futures import ThreadPoolExecutor

import os
import time

import airmtp
from camera_session import CameraSession, SessionError
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
    _log_services(client)

    items = None
    failures = []
    # "Select on Smartphone" browses PhotoRoot right away; the push variant
    # ("Select on camera") needs X_TransferStart first.  Which one this
    # camera is in is not known up front, so try both orders.
    for use_push in (False, True):
        if use_push:
            try:
                client.start_transfer()
                _diag("X_TransferStart ok")
            except SonySoapError as error:
                _diag("X_TransferStart rejected:", error)
                failures.append(f"TransferStart: {error}")
        for browse_root in ("PhotoRoot", "PushRoot", "0"):
            try:
                items = client._browse_tree(browse_root)
                _diag(f"browse {browse_root} (push={use_push}): {len(items)} items")
                if items:
                    break
            except SonySoapError as error:
                _diag(f"browse {browse_root} (push={use_push}) failed:", error)
                failures.append(f"{browse_root}: {error}")
        if items:
            break
    if not items:
        _probe_more(client)
        raise SonySoapError("; ".join(dict.fromkeys(failures)) or "the camera lists no pictures")
    _direct = client
    _items = items
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
        _diag("DigitalImagingDesc.xml:", raw.decode("utf-8", "replace").replace("\\n", " ")[:1500])
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


def thumbnail(index):
    if _direct is not None:
        resource = _smallest_resource(_items[int(index)])
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
        path = _direct.download(
            item, target, progress_cb=lambda done, total=None: listener.onProgress(done, total or expected)
        )
        return str(path)
    item = _files[int(index)]
    return _require().download(item, target, lambda done, total: listener.onProgress(done, total))


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
