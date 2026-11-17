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


def connect_direct(wait_seconds):
    """Connect to a camera in "Send to Smartphone" mode on its own Wi-Fi.

    The activity has already joined the camera's network and bound the app
    to it; the camera's web server can need a few seconds to answer.
    """

    global _direct, _items
    close()
    client = SonySoapClient(timeout=10.0)
    deadline = time.time() + float(wait_seconds)
    while True:
        try:
            descriptor = client.probe()
            break
        except SonySoapError:
            if time.time() > deadline:
                raise
            time.sleep(1.0)
    _log_services(client)
    try:
        client.start_transfer()
    except SonySoapError as error:
        # Not every model wants (or accepts) X_TransferStart; try browsing
        # anyway and only fail if that does not work either.
        print("camera-direct: X_TransferStart rejected:", error)
    _direct = client
    _items = _browse_direct(client)
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


def _browse_direct(client):
    errors = []
    for root in ("PushRoot", "PhotoRoot", "0"):
        try:
            items = client._browse_tree(root)
            print(f"camera-direct: browse {root}: {len(items)} items")
            if items or root == "0":
                return items
        except SonySoapError as error:
            print(f"camera-direct: browse {root} failed:", error)
            errors.append(f"{root}: {error}")
    raise SonySoapError("camera rejected every browse: " + "; ".join(errors))


def _log_services(client):
    """Print the camera's services and their actions to logcat (python.stdout).

    The "Send to Smartphone" protocol differs between models, so the log
    shows what this camera actually offers.  None of it is secret.
    """

    import xml.etree.ElementTree as ET
    from sony_soap import _first_text, _local_name

    try:
        root = ET.fromstring(client._request_bytes("GET", client.base_url + "/DmsDescPush.xml"))
    except Exception as error:  # diagnostics only
        print("camera-direct: descriptor failed:", error)
        return
    for service in root.iter():
        if _local_name(service.tag) != "service":
            continue
        kind = _first_text(service, "serviceType")
        scpd = _first_text(service, "SCPDURL")
        print("camera-direct: service", kind, "control", _first_text(service, "controlURL"), "scpd", scpd)
        if not scpd:
            continue
        try:
            doc = ET.fromstring(client._request_bytes("GET", client._camera_url(scpd)))
        except Exception as error:
            print("camera-direct:   scpd failed:", error)
            continue
        for action in doc.iter():
            if _local_name(action.tag) != "action":
                continue
            args = [
                f"{_first_text(arg, 'direction')}:{_first_text(arg, 'name')}"
                for arg in action.iter() if _local_name(arg.tag) == "argument"
            ]
            print("camera-direct:   action", _first_text(action, "name"), args)


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
