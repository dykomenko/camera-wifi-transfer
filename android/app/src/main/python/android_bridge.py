"""Calls from the Android activity into the shared camera session.

All calls come from one background thread of the activity, so no locking
is needed here beyond what ``CameraSession`` already does.
"""

import json

from camera_session import CameraSession, SessionError

_session = None
_files = []


def connect(work_dir, ip_address, interfaces_csv, attempts_per_interface):
    """Find the camera and list its files; returns JSON for the activity.

    ``interfaces_csv`` lists this phone's IPv4 addresses that may face the
    camera (Wi-Fi first, then the hotspot); discovery is tried on each.
    """

    global _session, _files
    close()
    ip_address = (ip_address or "").strip() or "auto"
    interfaces = [i for i in (interfaces_csv or "").split(",") if i] or [None]
    if ip_address != "auto":
        interfaces = [None]
    errors = []
    session = None
    for interface in interfaces:
        candidate = CameraSession(
            ip_address=ip_address,
            multicast_interface=interface,
            hostname="camera-transfer",
            work_dir=work_dir,
            discovery_attempts=int(attempts_per_interface),
        )
        try:
            candidate.connect()
            session = candidate
            break
        except SessionError as exc:
            errors.append("%s: %s" % (interface or ip_address, exc))
    if session is None:
        raise SessionError("; ".join(errors) or "camera not found")
    _session = session
    _files = session.list_files()
    capacity, free, label = session.storage()
    return json.dumps({
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
