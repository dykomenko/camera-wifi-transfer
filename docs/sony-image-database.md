# Sony image database (AVF_INFO) — research notes

Goal: move photos/videos off a Sony memory card **on the PC** (copy, verify,
delete) so that the camera afterwards shows no "image database" error and
needs no action on the camera.

Status: **research in progress, nothing writes to a card yet.**

Card studied: Sony ILCE-6500, 64 GB SDXC, exFAT. Reference data (card backup
and the database after a deletion made by the camera itself) is kept outside
the repository, in the owner's `Desktop\SD Backup` folder.

## Files on the card

| Path | Purpose |
|---|---|
| `DCIM\100MSDCF\DSCnnnnn.ARW/JPG` | photos |
| `AVF_INFO\AVIN0001.INP` | photo database, 32 MiB |
| `AVF_INFO\AVIN0001.BNP` | byte-identical backup of `.INP`; the camera writes both |
| `AVF_INFO\AVIN0001.INT` | 2.4 MB, all `0xFF` on this card; not touched by a deletion |
| `AVF_INFO\PRV00001.BIN` | per-photo list (little-endian), see below |
| `PRIVATE\M4ROOT\MEDIAPRO.XML` | XAVC S video catalogue (XML); empty when no clips |
| `PRIVATE\M4ROOT\CLIP, THMBNL, SUB` | video clips and their thumbnails/metadata |

## AVIN0001.INP (big-endian)

* Storage is in **128-byte units**. Unit number `n` (1-based) lives at
  offset `0x3F80 + n * 0x80` (unit 1 at `0x4000`).
* Unit header: byte `+0x00` = state (`1` = first unit of a record,
  `2` = continuation, `0` = free); `+0x0C` u16 = next unit of the chain
  (`0` = end). Payload starts at `+0x10` and continues in the next units.
* Free units keep their old bytes, so names of long-deleted photos remain in
  the file. Only the state byte matters.
* Record payload: u16 type, u32 length, u16, u32 parent unit, …
  * `0x0072` — item record, contains the path `100MSDCF/DSC04074.ARW`,
    capture time, GPS block (`WGS-84`).
  * `0x0390` — 7-unit metadata chain whose parent is the photo record.
  * `0x01C8` — group (most likely one per capture date): created/modified
    time, then a sub-record `u32 len, u16 0x0101, u16, u32 child[]`.
  * type `0x39..` top node with a photo counter (u32) and a list of groups.
* Each photo exists in **two trees** (two top nodes, two groups): one
  `0x0072` record under each group.

Header (unit 0 area, `0x0000-0x0140`): u32 at `0x1A` = database modified
time; u16 at `0xC0` = **highest used unit number** (equal to the used-unit
count only while there are no holes); a 16-byte database id at `0x3A`
(also at `PRV00001.BIN+0x04`).

Free-space bitmap: 128-byte pages (unit with u16 `0x80 + p` at `+0x0C`),
bitmap bytes from `+0x0E`, one bit per unit, MSB first, page `p` covers
units `p*256 … p*256+255`. Page 0x0A is at file offset `0x8400`.

### What the camera changed when it deleted DSC04089 (44 bytes)

1. Freed 10 units: photo record (1), its `0x0390` chain (7), the second-tree
   `0x0072` record (1) → state byte `0`.
   It also edited 4 length bytes inside those freed records (no effect once
   free).
2. Cleared the same 10 bits in the bitmap (`ff ff` → `fc 00`).
3. Header used-unit count `0x0AD0` → `0x0AC6` (−10).
4. In both groups: removed the child id from the list (following bytes
   shifted left by 4), record length −4, list length −4, modified time = now.
5. In both top nodes: photo counter 16 → 15.
6. Header modified time = now. `.BNP` = identical copy of the new `.INP`.

Timestamps are u32 seconds; the value is consistent across records but its
epoch is not plain Unix time on this camera.

### Record links (confirmed on 16 photos)

* Photo record `M` (type `0x0072`, len `0x6A`, contains the path) → parent =
  date group in tree 1; sub-record `0x0101` holds its `0x0390` metadata chain.
* Twin record `R` (type `0x0072`, len `0x5E`, no path) → parent = date group in
  tree 2; its sub-record `u32 0x0A, u16 0x0104, u32 M` points back to `M`.
* Group (type `0x01C8`): sub-record `u32 len, u16 0x0101, u16, u32 child[]`.
* Top node: sub-record `u32 0x0C, u16 0x0127, u32 photo_count`, then
  `u32 len, u16 0x0101, u8 0x02, u32 group[]`; also a modified time.
  Photo top nodes: `0x921` (tree 1) and `0x9AE` (tree 2); video top node `0x736`.
* Removing an id from any list: cut 4 bytes, shift the rest of the record
  left (across the unit chain), record length −4, list length −4. Bytes past
  the new end are left as they were.

### Deleting every photo (second experiment)

Done in the camera, partly with "delete all images of this date". Result:
both date groups in both trees were **freed** and removed from their top
node lists (top node length `0x3C` → `0x34`), both photo counters 16 → 0,
`DCIM@MSDCF`, `DCIM` and `MP_ROOT@ANV01` folders were deleted.

### Video

A clip is a group (type `0x01C8`) under the video top node `0x736`, an item
record (type `0x00E4`, path `CLIP/C0001.MP4`) and a `0x0390` chain.
Deleting a clip in the camera also removes `CLIP\CnnnnM01.XML`,
`THMBNL\CnnnnT01.JPG` and the clip's `<Material>` element from
`MEDIAPRO.XML`, which keeps a fixed file size (padded with spaces) and gets a
new `createdAt` time.

## PRV00001.BIN (little-endian)

* `0x00` u32 photo count (16 → 15); `0x04` 16-byte database id.
* `0x16` modified time as `u16 year, u8 month, day, hour, min, sec` (UTC).
* `0x2A` time of the newest photo (same format).
* `0x28-0x3B` "newest date" block: u16 `0x3C`, date-time of the newest
  photo, 3 unknown bytes at `0x31`, u32 `2`, u32 photos in that date (5 → 4).
  **All zero once no photos are left**, so moving every photo needs no
  knowledge of the unknown bytes. `0xE0` u32 = that date's tree-2 group id
  (zero once empty).
* `0x124` date-time of the newest item (local time).
* `0x134` u32: photo count again (16 → 15).
* From `0x200`: 36-byte entries, one per photo, containing the second-tree
  unit id of the photo. The deleted entry was cut out and the rest shifted;
  stale bytes stay after the end of the list.
* **Not understood yet:** 3 bytes at `0x31` (`88 ef 04` → `6b d7 19`; not
  CRC32/Adler32/byte sum over any tested range) and u32 at `0x130` (0 → 3).

## Still needed

* The unknown 3 bytes in the "newest date" block of `PRV00001.BIN` — only
  needed when some photos stay on the card.
* Exact `PRV00001.BIN` entry layout for videos.
* Final check: a deletion done by the PC, then the card is inserted into the
  camera and must show no error.
