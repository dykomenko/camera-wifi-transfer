"""Native Windows USB pairing for Sony ``Send to Computer``.

The old cross-platform helper talks to USB endpoints through libusb.  That
cannot open a camera owned by Windows' normal ``USBSTOR`` driver.  This module
instead uses the documented Windows SCSI pass-through interface, just like the
native Windows MSC backend in Sony-PMCA-RE.  It never installs or replaces a
driver and it only addresses the small ``PMHOME`` service LUN, not the memory
card containing photographs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import string
from typing import Any


IOCTL_SCSI_PASS_THROUGH = 0x4D004
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
SCSI_IOCTL_DATA_OUT = 0
SCSI_IOCTL_DATA_IN = 1
DRIVE_REMOVABLE = 2

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_SUPPORTED = 50

PAIRING_CDB = bytes((0x7A, 0x04, 0, 0, 0, 0, 0, 0, 0))
GUID1 = bytes((0xFF, 0xFF, 0x52, 0x54, 0x00, 0xB6, 0xFD, 0xA9))
GUID2 = bytes((0xFF, 0xFF, 0x52, 0x3C, 0x28, 0x07, 0xA9, 0x3A))


class PairingError(RuntimeError):
    """Base class for a safe, user-presentable pairing failure."""


class PairingNeedsElevation(PairingError):
    """The service volume could not be opened with read/write access."""


class ScsiStatusError(PairingError):
    """Windows delivered the CDB but the camera rejected it."""


class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", wintypes.ULONG),
        ("TimeOutValue", wintypes.ULONG),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", wintypes.ULONG),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


class SCSI_PASS_THROUGH_DIRECT_WITH_BUFFER(ctypes.Structure):
    _fields_ = [
        ("sptd", SCSI_PASS_THROUGH_DIRECT),
        ("Filler", wintypes.ULONG),
        ("Sense", ctypes.c_ubyte * 64),
    ]


class SCSI_PASS_THROUGH(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", wintypes.ULONG),
        ("TimeOutValue", wintypes.ULONG),
        ("DataBufferOffset", ctypes.c_size_t),
        ("SenseInfoOffset", wintypes.ULONG),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


@dataclass(frozen=True, slots=True)
class VolumeCandidate:
    root: str
    device_path: str
    label: str


@dataclass(frozen=True, slots=True)
class InquiryInfo:
    vendor: str
    product: str
    revision: str

    @property
    def is_sony_camera(self) -> bool:
        return self.vendor.upper().startswith("SONY") and self.product.upper().startswith("DSC")


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetLogicalDrives.restype = wintypes.DWORD
    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT
    _kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    _kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    _kernel32.VirtualAlloc.restype = ctypes.c_void_p
    _kernel32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    _kernel32.VirtualFree.restype = wintypes.BOOL
    _shell32.IsUserAnAdmin.restype = wintypes.BOOL


def is_elevated() -> bool:
    if os.name != "nt":
        return False
    return bool(_shell32.IsUserAnAdmin())


def build_pairing_payload(hostname: str = "CAMERAPC") -> bytes:
    """Build the exact 8192-byte ForceRegistHostNetInfo request."""

    payload = bytearray(8192)
    payload[0:5] = b"\x25\x01\x00\x00\x08"
    encoded_name = hostname.encode("utf-16le", errors="ignore")[:16]
    payload[17 : 17 + len(encoded_name)] = encoded_name
    payload[177:181] = b"\x10\x00\x00\x00"
    payload[181:189] = GUID1
    payload[189:197] = GUID2
    return bytes(payload)


def parse_inquiry(data: bytes) -> InquiryInfo:
    if len(data) < 36:
        raise PairingError("The camera returned a short SCSI INQUIRY response")
    return InquiryInfo(
        vendor=data[8:16].decode("ascii", errors="replace").rstrip(" \x00"),
        product=data[16:32].decode("ascii", errors="replace").rstrip(" \x00"),
        revision=data[32:36].decode("ascii", errors="replace").rstrip(" \x00"),
    )


def list_pmhome_volumes() -> list[VolumeCandidate]:
    """Return removable volumes carrying Sony's small PMHOME service LUN."""

    if os.name != "nt":
        return []
    mask = int(_kernel32.GetLogicalDrives())
    found: list[VolumeCandidate] = []
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        if int(_kernel32.GetDriveTypeW(root)) != DRIVE_REMOVABLE:
            continue
        label_buffer = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maximum_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        filesystem = ctypes.create_unicode_buffer(261)
        ok = _kernel32.GetVolumeInformationW(
            root,
            label_buffer,
            len(label_buffer),
            ctypes.byref(serial),
            ctypes.byref(maximum_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        )
        label = label_buffer.value if ok else ""
        if label.upper() == "PMHOME":
            found.append(VolumeCandidate(root, rf"\\.\{letter}:", label))
    return found


def _open_volume(device_path: str) -> int:
    handle = _kernel32.CreateFileW(
        device_path,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        if error == ERROR_ACCESS_DENIED:
            raise PairingNeedsElevation("Sony USB pairing requires Windows UAC confirmation")
        raise OSError(error, ctypes.FormatError(error), device_path)
    return int(handle)


def _raise_scsi_status(status: int, sense: bytes) -> None:
    if status == 0:
        return
    compact = sense.hex(" ").upper()
    raise ScsiStatusError(
        f"The camera rejected the USB command: SCSI status {status}, sense {compact}"
    )


def _send_scsi_direct(handle: int, cdb: bytes, direction: int, data: bytes) -> tuple[bytes, str]:
    length = len(data)
    allocation_size = max(1, length)
    data_pointer = _kernel32.VirtualAlloc(
        None, allocation_size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
    )
    if not data_pointer:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    try:
        if direction == SCSI_IOCTL_DATA_OUT and data:
            ctypes.memmove(data_pointer, data, length)

        packet = SCSI_PASS_THROUGH_DIRECT_WITH_BUFFER()
        packet.sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        packet.sptd.CdbLength = len(cdb)
        packet.sptd.SenseInfoLength = len(packet.Sense)
        packet.sptd.DataIn = direction
        packet.sptd.DataTransferLength = length
        packet.sptd.TimeOutValue = 10
        packet.sptd.DataBuffer = data_pointer
        packet.sptd.SenseInfoOffset = SCSI_PASS_THROUGH_DIRECT_WITH_BUFFER.Sense.offset
        packet.sptd.Cdb = (ctypes.c_ubyte * 16).from_buffer_copy(cdb.ljust(16, b"\0"))

        returned = wintypes.DWORD()
        packet_size = ctypes.sizeof(packet)
        ok = _kernel32.DeviceIoControl(
            handle,
            IOCTL_SCSI_PASS_THROUGH_DIRECT,
            ctypes.byref(packet),
            packet_size,
            ctypes.byref(packet),
            packet_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        _raise_scsi_status(packet.sptd.ScsiStatus, bytes(packet.Sense))
        transferred = min(length, int(packet.sptd.DataTransferLength))
        response = ctypes.string_at(data_pointer, transferred) if direction == SCSI_IOCTL_DATA_IN else b""
        return response, "direct"
    finally:
        _kernel32.VirtualFree(data_pointer, 0, MEM_RELEASE)


def _send_scsi_buffered(handle: int, cdb: bytes, direction: int, data: bytes) -> tuple[bytes, str]:
    data_offset = 512
    total_size = data_offset + len(data)
    raw = (ctypes.c_ubyte * total_size)()
    packet = SCSI_PASS_THROUGH.from_buffer(raw)
    packet.Length = ctypes.sizeof(SCSI_PASS_THROUGH)
    packet.CdbLength = len(cdb)
    packet.SenseInfoLength = 64
    packet.DataIn = direction
    packet.DataTransferLength = len(data)
    packet.TimeOutValue = 10
    packet.DataBufferOffset = data_offset
    packet.SenseInfoOffset = ctypes.sizeof(SCSI_PASS_THROUGH)
    packet.Cdb = (ctypes.c_ubyte * 16).from_buffer_copy(cdb.ljust(16, b"\0"))
    if direction == SCSI_IOCTL_DATA_OUT and data:
        ctypes.memmove(ctypes.addressof(raw) + data_offset, data, len(data))

    returned = wintypes.DWORD()
    ok = _kernel32.DeviceIoControl(
        handle,
        IOCTL_SCSI_PASS_THROUGH,
        ctypes.byref(raw),
        total_size,
        ctypes.byref(raw),
        total_size,
        ctypes.byref(returned),
        None,
    )
    if not ok:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    sense_start = int(packet.SenseInfoOffset)
    sense = bytes(raw[sense_start : sense_start + int(packet.SenseInfoLength)])
    _raise_scsi_status(packet.ScsiStatus, sense)
    transferred = min(len(data), int(packet.DataTransferLength))
    response = bytes(raw[data_offset : data_offset + transferred]) if direction == SCSI_IOCTL_DATA_IN else b""
    return response, "buffered"


def _send_scsi(handle: int, cdb: bytes, direction: int, data: bytes) -> tuple[bytes, str]:
    try:
        return _send_scsi_direct(handle, cdb, direction, data)
    except OSError as exc:
        if getattr(exc, "winerror", exc.errno) not in (ERROR_INVALID_PARAMETER, ERROR_NOT_SUPPORTED):
            raise
        return _send_scsi_buffered(handle, cdb, direction, data)


def pair_camera(hostname: str | None = None) -> dict[str, Any]:
    """Find a single Sony PMHOME LUN, verify it, and register our PTP/IP GUID."""

    if os.name != "nt":
        raise PairingError("Sony USB pairing is supported on Windows only")
    if not is_elevated():
        raise PairingNeedsElevation("Sony USB pairing requires Windows UAC confirmation")

    candidates = list_pmhome_volumes()
    if not candidates:
        raise PairingError(
            "The PMHOME service volume was not found. Turn the camera on, select USB Mass Storage, and reconnect the cable."
        )
    if len(candidates) != 1:
        drives = ", ".join(candidate.root for candidate in candidates)
        raise PairingError(f"Multiple PMHOME volumes were found ({drives}); disconnect other cameras")

    candidate = candidates[0]
    handle = _open_volume(candidate.device_path)
    try:
        inquiry_cdb = bytes((0x12, 0, 0, 0, 96, 0))
        inquiry_data, inquiry_method = _send_scsi(
            handle, inquiry_cdb, SCSI_IOCTL_DATA_IN, bytes(96)
        )
        info = parse_inquiry(inquiry_data)
        if not info.is_sony_camera:
            raise PairingError(
                f"Volume {candidate.root} did not pass the Sony DSC check: {info.vendor} {info.product}"
            )

        host = (hostname or os.environ.get("COMPUTERNAME") or "CAMERAPC")[:8]
        _unused, pairing_method = _send_scsi(
            handle, PAIRING_CDB, SCSI_IOCTL_DATA_OUT, build_pairing_payload(host)
        )
    finally:
        _kernel32.CloseHandle(handle)

    return {
        "success": True,
        "drive": candidate.root,
        "vendor": info.vendor,
        "product": info.product,
        "revision": info.revision,
        "inquiry_method": inquiry_method,
        "pairing_method": pairing_method,
        "guid": "0xa9fdb6005452ffff-0x3aa907283c52ffff",
    }


__all__ = [
    "GUID1",
    "GUID2",
    "PAIRING_CDB",
    "InquiryInfo",
    "PairingError",
    "PairingNeedsElevation",
    "build_pairing_payload",
    "is_elevated",
    "list_pmhome_volumes",
    "pair_camera",
    "parse_inquiry",
]
