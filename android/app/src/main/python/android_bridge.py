"""Calls from the Android activity into the shared camera session.

All calls come from one background thread of the activity, so no locking
is needed here beyond what ``CameraSession`` already does.
"""

import json
import socket
from concurrent.futures import ThreadPoolExecutor

import airmtp
from camera_session import CameraSession, SessionError

PTPIP_PORT = 15740

_session = None
_files = []


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


def thumbnail(index):
    return _require().thumbnail(_files[int(index)])


def download(index, target, listener):
    """Copy file ``index`` to ``target``; ``listener.onProgress(done, total)``."""

    item = _files[int(index)]
    return _require().download(item, target, lambda done, total: listener.onProgress(done, total))


def is_connected():
    return _session is not None and _session.connected


def close():
    global _session, _files
    session, _session, _files = _session, None, []
    if session is not None:
        session.close(sleep_camera=True)


def _require():
    if _session is None:
        raise SessionError("not connected")
    return _session
