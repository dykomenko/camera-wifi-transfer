"""Read and edit the image database of a Sony camera memory card.

Sony cameras keep an index of the photos and videos on a card in
``AVF_INFO\\AVIN0001.INP`` (with a byte-identical backup ``AVIN0001.BNP``)
and ``AVF_INFO\\PRV00001.BIN``.  Deleting media files on a PC leaves that
index pointing at missing files, and the camera then reports a database
error.  This module removes items from the index the same way the camera
does when it deletes them itself.  The format notes are in
``docs/sony-image-database.md``.

The module is deliberately strict: :meth:`SonyCardDatabase.validate` checks
that every structure it relies on is consistent, and every edit refuses to
run if anything does not match what was learned from a real camera.  It
never touches files on the card; callers read and write the bytes.
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field

UNIT = 0x80
UNIT_BASE = 0x3F80  # offset of unit n is UNIT_BASE + n * UNIT; unit 1 at 0x4000
UNIT_HEADER = 0x0E  # state byte, zeros, u16 next unit at +0x0C
UNIT_PAYLOAD = UNIT - UNIT_HEADER  # 0x72 bytes of record data per unit
STATE_FREE, STATE_HEAD, STATE_CONTINUATION = 0, 1, 2

HEADER_TIME = 0x1A  # u32, database modified time
HEADER_LAST_UNIT = 0xC0  # u16, highest used unit number

TYPE_CONTAINER = 0x0002  # top nodes and date groups
TYPE_ITEM = 0x0011  # photo, its twin in the second tree, or video
KIND_PHOTO = 0x0002
KIND_VIDEO = 0x0003
KIND_TWIN = 0x8000

TAG_CHILDREN = 0x0101
TAG_COUNT = 0x0127
TAG_TWIN = 0x0104

# Record layout (big-endian): u32 capacity, u32 length, u16 type,
# u32 parent, u16 kind, u32 created, u32 modified, then sub-records.
LENGTH_OFFSET = 4
TYPE_OFFSET = 8
PARENT_OFFSET = 10
KIND_OFFSET = 14
CREATED_OFFSET = 16
MODIFIED_OFFSET = 20
SUBRECORDS_OFFSET = 24

BITMAP_HEADER = 22  # bytes before the free-space bitmap in its record payload

# Seconds between 1970-01-01 and the epoch the camera uses (1972-01-01).
CAMERA_EPOCH_OFFSET = 730 * 86400

# PRV00001.BIN (little-endian)
PRV_COUNT = 0x00
PRV_MODIFIED_UTC = 0x16
PRV_NEWEST_PHOTO = (0x28, 0x3C)  # block cleared once no photos are left
PRV_NEWEST_PHOTO_GROUP = 0xE0
PRV_NEWEST_VIDEO = (0xA0, 0xB4)
PRV_NEWEST_VIDEO_GROUP = 0x10C
PRV_LAST_OPERATION = 0x130
PRV_PHOTO_COUNT = 0x134
PRV_VIDEO_COUNT = 0x14C
PRV_LIST = 0x200
PRV_ENTRY = 0x24
PRV_ENTRY_KEY = 0x0C
PRV_LINK = 0x0C
PRV_LINK_KEY = 0x08
PRV_OPERATION_DELETE = 3


class DatabaseError(Exception):
    """The database does not look like what this module understands."""


@dataclass
class Record:
    number: int
    type: int
    length: int
    parent: int
    kind: int
    units: list[int]
    payload: bytearray

    def subrecords(self) -> list[tuple[int, int, int]]:
        """Return ``(offset, length, tag)`` for each sub-record."""

        result = []
        position = SUBRECORDS_OFFSET
        while position + 6 <= self.length:
            size, tag = struct.unpack_from(">IH", self.payload, position)
            if size < 6 or position + size > self.length:
                break
            result.append((position, size, tag))
            position += size
        return result

    def subrecord(self, tag: int) -> tuple[int, int]:
        for position, size, found in self.subrecords():
            if found == tag:
                return position, size
        raise DatabaseError(f"record {self.number:#x} has no sub-record {tag:#06x}")

    def children(self) -> list[int]:
        position, size = self.subrecord(TAG_CHILDREN)
        count = (size - 8) // 4
        return list(struct.unpack_from(f">{count}I", self.payload, position + 8))


@dataclass
class MediaItem:
    """A photo or video as the camera indexes it."""

    path: str  # relative to the card root, e.g. DCIM/100MSDCF/DSC04090.ARW
    kind: str  # "photo" or "video"
    record: int
    twin: int | None  # second-tree record of a photo
    group: int
    twin_group: int | None
    records_to_free: list[int] = field(default_factory=list)

    @property
    def key(self) -> int:
        """Id used for the item in PRV00001.BIN."""

        return self.twin if self.twin is not None else self.record


def camera_time(moment: _dt.datetime) -> int:
    return int(moment.timestamp()) - CAMERA_EPOCH_OFFSET


class SonyCardDatabase:
    def __init__(self, inp: bytes, bnp: bytes, prv: bytes) -> None:
        if inp != bnp:
            raise DatabaseError("AVIN0001.INP and its backup AVIN0001.BNP differ")
        self.inp = bytearray(inp)
        self.prv = bytearray(prv)
        self.records = self._read_records()
        self.items = self._read_items()
        self.bitmap_record = self._find_bitmap()
        self.validate()

    # ------------------------------------------------------------ reading
    def _unit_offset(self, number: int) -> int:
        return UNIT_BASE + number * UNIT

    def _state(self, number: int) -> int:
        return self.inp[self._unit_offset(number)]

    def _next(self, number: int) -> int:
        offset = self._unit_offset(number)
        return struct.unpack_from(">H", self.inp, offset + 0x0C)[0]

    def _unit_count(self) -> int:
        return (len(self.inp) - UNIT_BASE) // UNIT - 1

    def _read_records(self) -> dict[int, Record]:
        records: dict[int, Record] = {}
        owner: dict[int, int] = {}
        for number in range(1, self._unit_count() + 1):
            if self._state(number) != STATE_HEAD:
                continue
            units = []
            current = number
            while current:
                if current in owner or current in units:
                    raise DatabaseError(f"unit {current:#x} belongs to two records")
                expected = STATE_HEAD if not units else STATE_CONTINUATION
                if self._state(current) != expected:
                    raise DatabaseError(f"broken chain at unit {current:#x}")
                units.append(current)
                current = self._next(current)
            payload = bytearray()
            for unit in units:
                offset = self._unit_offset(unit) + UNIT_HEADER
                payload += self.inp[offset : offset + UNIT_PAYLOAD]
            capacity, length, record_type, parent, kind = struct.unpack_from(">IIHIH", payload, 0)
            if capacity != len(units) * UNIT_PAYLOAD or length > capacity:
                raise DatabaseError(f"record {number:#x}: size fields do not match its units")
            for unit in units:
                owner[unit] = number
            records[number] = Record(number, record_type, length, parent, kind, units, payload)
        for number in range(1, self._unit_count() + 1):
            if self._state(number) == STATE_CONTINUATION and number not in owner:
                raise DatabaseError(f"orphan continuation unit {number:#x}")
        return records

    def _path_of(self, record: Record) -> str | None:
        data = bytes(record.payload[: record.length])
        for prefix, root in ((b"100MSDCF/", "DCIM/"), (b"CLIP/", "PRIVATE/M4ROOT/")):
            start = data.find(prefix)
            if start < 0:
                continue
            # The path is preceded by a length-free marker byte and ends at NUL.
            end = data.find(b"\x00", start)
            text = data[start:end].decode("ascii", "strict")
            if root == "DCIM/":
                return root + text
            return root + text
        return None

    def _read_items(self) -> list[MediaItem]:
        twins: dict[int, int] = {}
        link = struct.pack(">IH", 0x0A, TAG_TWIN)
        for record in self.records.values():
            if record.type != TYPE_ITEM:
                continue
            # The twin's sub-records do not start at SUBRECORDS_OFFSET, so look
            # for the exact link sub-record: u32 length 10, tag 0x0104, u32 id.
            data = bytes(record.payload[: record.length])
            position = data.find(link)
            if record.kind != KIND_TWIN:
                continue
            if position >= 0 and data.find(link, position + 1) < 0:
                target = struct.unpack_from(">I", data, position + 6)[0]
                if target in twins:
                    raise DatabaseError(f"record {target:#x} has two twins")
                twins[target] = record.number
        items = []
        for record in self.records.values():
            if record.type != TYPE_ITEM or record.kind not in (KIND_PHOTO, KIND_VIDEO):
                continue
            path = self._path_of(record)
            if path is None:
                raise DatabaseError(f"item record {record.number:#x} has no file path")
            kind = "photo" if record.kind == KIND_PHOTO else "video"
            twin = twins.get(record.number) if kind == "photo" else None
            if kind == "photo" and twin is None:
                raise DatabaseError(f"photo {path} has no second-tree record")
            to_free = [record.number]
            to_free += self._descendants(record.number)
            if twin is not None:
                to_free.append(twin)
                to_free += self._descendants(twin)
            items.append(
                MediaItem(
                    path=path,
                    kind=kind,
                    record=record.number,
                    twin=twin,
                    group=record.parent,
                    twin_group=self.records[twin].parent if twin is not None else None,
                    records_to_free=to_free,
                )
            )
        return sorted(items, key=lambda item: item.path)

    def _descendants(self, number: int) -> list[int]:
        found = []
        for record in self.records.values():
            if record.parent == number:
                if record.type == TYPE_CONTAINER:
                    raise DatabaseError(f"item {number:#x} unexpectedly owns a container")
                found.append(record.number)
                found += self._descendants(record.number)
        return found

    def _find_bitmap(self) -> int:
        used = {u for r in self.records.values() for u in r.units}
        for record in self.records.values():
            payload = record.payload
            needed = BITMAP_HEADER + (self._unit_count() + 7) // 8
            if len(payload) < needed:
                continue
            ok = True
            set_bits = 0
            for number in range(1, self._unit_count() + 1):
                index = number - 1
                bit = payload[BITMAP_HEADER + index // 8] >> (7 - index % 8) & 1
                set_bits += bit
                if bit != (number in used):
                    ok = False
                    break
            if ok and set_bits == len(used):
                return record.number
        raise DatabaseError("free-space bitmap not found or inconsistent")

    # --------------------------------------------------------- validation
    def validate(self) -> None:
        used = [u for r in self.records.values() for u in r.units]
        last = struct.unpack_from(">H", self.inp, HEADER_LAST_UNIT)[0]
        if last != max(used):
            raise DatabaseError(f"header last unit {last:#x} != highest used unit {max(used):#x}")
        groups_by_top: dict[int, set[int]] = {}
        for item in self.items:
            for group_number in (item.group, item.twin_group):
                if group_number is not None and group_number in self.records:
                    top = self.records[group_number].parent
                    groups_by_top.setdefault(top, set()).add(group_number)
        for item in self.items:
            for record_number, group_number in (
                (item.record, item.group),
                (item.twin, item.twin_group),
            ):
                if record_number is None:
                    continue
                group = self.records.get(group_number)
                if group is None or group.type != TYPE_CONTAINER:
                    raise DatabaseError(f"{item.path}: parent {group_number:#x} is not a group")
                if record_number not in group.children():
                    raise DatabaseError(f"{item.path}: not listed in group {group_number:#x}")
                top = self.records.get(group.parent)
                if top is None:
                    raise DatabaseError(f"group {group_number:#x} has no top node")
                if group_number not in top.children():
                    raise DatabaseError(f"group {group_number:#x} not listed in its top node")
        for top_number, groups in groups_by_top.items():
            top = self.records[top_number]
            members = sum(
                1
                for item in self.items
                for record_number, group in ((item.record, item.group), (item.twin, item.twin_group))
                if record_number is not None and group in groups
            )
            if self._top_count(top) != members:
                raise DatabaseError(
                    f"top node {top_number:#x} counts {self._top_count(top)}, found {members}"
                )
        photos = sum(item.kind == "photo" for item in self.items)
        videos = sum(item.kind == "video" for item in self.items)
        prv_count, = struct.unpack_from("<I", self.prv, PRV_COUNT)
        prv_photos, = struct.unpack_from("<I", self.prv, PRV_PHOTO_COUNT)
        prv_videos, = struct.unpack_from("<I", self.prv, PRV_VIDEO_COUNT)
        if (prv_count, prv_photos, prv_videos) != (photos + videos, photos, videos):
            raise DatabaseError(
                f"PRV00001.BIN counts {prv_count}/{prv_photos}/{prv_videos}, "
                f"database has {photos + videos}/{photos}/{videos}"
            )
        keys = [self._prv_key(i) for i in range(prv_count)]
        if sorted(keys) != sorted(item.key for item in self.items):
            raise DatabaseError("PRV00001.BIN list does not match the database items")
        links = [self._prv_link_key(i, prv_count) for i in range(prv_count)]
        if sorted(links) != sorted(item.key for item in self.items):
            raise DatabaseError("PRV00001.BIN link list does not match the database items")

    def _top_count(self, top: Record) -> int:
        position, _size = top.subrecord(TAG_COUNT)
        return struct.unpack_from(">I", top.payload, position + 8)[0]

    def _prv_key(self, index: int) -> int:
        return struct.unpack_from("<I", self.prv, PRV_LIST + index * PRV_ENTRY + PRV_ENTRY_KEY)[0]

    def _prv_link_key(self, index: int, count: int) -> int:
        base = PRV_LIST + count * PRV_ENTRY
        return struct.unpack_from("<I", self.prv, base + index * PRV_LINK + PRV_LINK_KEY)[0]

    # ------------------------------------------------------------ editing
    def _write_record(self, record: Record) -> None:
        for index, unit in enumerate(record.units):
            offset = self._unit_offset(unit) + UNIT_HEADER
            chunk = record.payload[index * UNIT_PAYLOAD : (index + 1) * UNIT_PAYLOAD]
            self.inp[offset : offset + UNIT_PAYLOAD] = chunk

    def _remove_child(self, record: Record, child: int, now: int) -> int:
        """Cut ``child`` out of the record's list; return how many remain."""

        position, size = record.subrecord(TAG_CHILDREN)
        children = record.children()
        index = children.index(child)
        start = position + 8 + index * 4
        end = record.length
        record.payload[start : end - 4] = record.payload[start + 4 : end]
        struct.pack_into(">I", record.payload, LENGTH_OFFSET, record.length - 4)
        struct.pack_into(">I", record.payload, position, size - 4)
        struct.pack_into(">I", record.payload, MODIFIED_OFFSET, now)
        record.length -= 4
        self._write_record(record)
        return len(children) - 1

    def _free(self, number: int) -> None:
        record = self.records.pop(number)
        bitmap = self.records[self.bitmap_record]
        for unit in record.units:
            self.inp[self._unit_offset(unit)] = STATE_FREE
            index = unit - 1
            bitmap.payload[BITMAP_HEADER + index // 8] &= ~(0x80 >> (index % 8)) & 0xFF
        self._write_record(bitmap)

    def delete(self, paths: list[str], moment: _dt.datetime, *, _allow_partial: bool = False) -> None:
        """Remove the given items from the index, as the camera does.

        Only removing every photo and/or every video is supported: the
        "newest photo/video" summaries in PRV00001.BIN contain fields that
        cannot be recomputed yet.  ``_allow_partial`` exists for tests that
        replay a deletion made by a camera; it leaves those summaries alone.
        """

        wanted = set(paths)
        by_path = {item.path: item for item in self.items}
        missing = wanted - by_path.keys()
        if missing:
            raise DatabaseError(f"not in the database: {sorted(missing)}")
        remaining_photos = [i for i in self.items if i.kind == "photo" and i.path not in wanted]
        remaining_videos = [i for i in self.items if i.kind == "video" and i.path not in wanted]
        removed = [by_path[p] for p in sorted(wanted)]
        # The "newest photo/video" summaries in PRV00001.BIN contain fields we
        # cannot recompute yet, so a kind is either kept whole or removed whole.
        for kind, remaining in (("photo", remaining_photos), ("video", remaining_videos)):
            if remaining and any(item.kind == kind for item in removed) and not _allow_partial:
                raise DatabaseError(f"only moving every {kind} on the card is supported")

        now = camera_time(moment)
        prv_before = struct.unpack_from("<I", self.prv, PRV_COUNT)[0]
        for item in removed:
            for record_number, group_number in ((item.record, item.group), (item.twin, item.twin_group)):
                if record_number is None:
                    continue
                group = self.records[group_number]
                top = self.records[group.parent]
                if self._remove_child(group, record_number, now) == 0:
                    # Like the camera: an empty group is removed altogether.
                    self._remove_child(top, group_number, now)
                    self._free(group_number)
                position, _size = top.subrecord(TAG_COUNT)
                count = struct.unpack_from(">I", top.payload, position + 8)[0]
                struct.pack_into(">I", top.payload, position + 8, count - 1)
                self._write_record(top)
            for number in item.records_to_free:
                self._free(number)

        used = [u for r in self.records.values() for u in r.units]
        struct.pack_into(">H", self.inp, HEADER_LAST_UNIT, max(used))
        struct.pack_into(">I", self.inp, HEADER_TIME, now)

        self._prv_remove([item.key for item in removed], prv_before)
        newest_group = struct.unpack_from("<I", self.prv, PRV_NEWEST_PHOTO_GROUP)[0]
        in_newest = sum(1 for item in removed if item.twin_group == newest_group)
        if remaining_photos and in_newest:
            count_offset = PRV_NEWEST_PHOTO[1] - 4
            count = struct.unpack_from("<I", self.prv, count_offset)[0]
            struct.pack_into("<I", self.prv, count_offset, count - in_newest)
        if not remaining_photos:
            self.prv[PRV_NEWEST_PHOTO[0] : PRV_NEWEST_PHOTO[1]] = bytes(PRV_NEWEST_PHOTO[1] - PRV_NEWEST_PHOTO[0])
            struct.pack_into("<I", self.prv, PRV_NEWEST_PHOTO_GROUP, 0)
        if not remaining_videos:
            self.prv[PRV_NEWEST_VIDEO[0] : PRV_NEWEST_VIDEO[1]] = bytes(PRV_NEWEST_VIDEO[1] - PRV_NEWEST_VIDEO[0])
            struct.pack_into("<I", self.prv, PRV_NEWEST_VIDEO_GROUP, 0)
        struct.pack_into("<I", self.prv, PRV_PHOTO_COUNT, len(remaining_photos))
        struct.pack_into("<I", self.prv, PRV_VIDEO_COUNT, len(remaining_videos))
        struct.pack_into("<I", self.prv, PRV_LAST_OPERATION, PRV_OPERATION_DELETE)
        utc = moment.astimezone(_dt.timezone.utc)
        struct.pack_into(
            "<HBBBBB", self.prv, PRV_MODIFIED_UTC,
            utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second,
        )

        self.items = [item for item in self.items if item.path not in wanted]
        self.validate()

    def _prv_remove(self, keys: list[int], count: int) -> None:
        entries = [
            bytes(self.prv[PRV_LIST + i * PRV_ENTRY : PRV_LIST + (i + 1) * PRV_ENTRY]) for i in range(count)
        ]
        links_base = PRV_LIST + count * PRV_ENTRY
        links = [bytes(self.prv[links_base + i * PRV_LINK : links_base + (i + 1) * PRV_LINK]) for i in range(count)]
        key_set = set(keys)
        kept_entries = [e for e in entries if struct.unpack_from("<I", e, PRV_ENTRY_KEY)[0] not in key_set]
        kept_links = [l for l in links if struct.unpack_from("<I", l, PRV_LINK_KEY)[0] not in key_set]
        if len(kept_entries) != count - len(keys) or len(kept_links) != count - len(keys):
            raise DatabaseError("PRV00001.BIN entries for the removed items not found")
        new = b"".join(kept_entries) + b"".join(kept_links)
        # Like the camera: compact the lists and leave older bytes after them.
        self.prv[PRV_LIST : PRV_LIST + len(new)] = new
        struct.pack_into("<I", self.prv, PRV_COUNT, len(kept_entries))

    def to_bytes(self) -> tuple[bytes, bytes, bytes]:
        """Return ``(INP, BNP, PRV)``; INP and BNP are identical, as on a camera."""

        return bytes(self.inp), bytes(self.inp), bytes(self.prv)
