# Changelog

## 0.3.4-beta — 2026-10-05

- The transfer now starts automatically when the app opens. Controlled by the new "Start downloading when the app opens" option (on by default).
- The completion summary now checks that every file found on the camera was either downloaded or is already in the folder, and warns not to erase the card if any are missing.
- The completion dialog offers to open the download folder.
- The window title shows overall progress, so it is visible in the taskbar while minimized.
- Fixed headings rendering at body-text size: a global font option was overriding the title and section styles.
- Fixed mismatched label backgrounds inside the instruction and progress cards; aligned button heights.

## 0.3.3-beta — 2026-09-05

- Fixed a bug (PTP/IP mode) where a photo could be silently marked "already downloaded" and skipped even though it was no longer in the destination folder — for example after it was manually deleted, moved, or backed up elsewhere. The backend kept its own persistent history of every filename it had ever transferred and trusted that ledger instead of checking the destination folder, which risked photos being erased from the camera without ever actually reaching the PC.
- The duplicate check now looks only at the destination folder: a file is skipped solely when it is still physically present there. No history ledger is consulted or trusted anymore.

## 0.3.2-beta — 2026-08-21

- Fixed the MTP StorageInfo parser to read StorageDescription and VolumeLabel as separate strings.
- Shows a localized generic memory-card label when the camera does not expose its user-assigned volume name.
- Removed the camera-side selected-transfer action and its UI at user request.
- Verified the corrected storage fields live on Sony ILCE-6500.

## 0.3.1-beta — 2026-08-21

- Fixed a Windows encoding crash when a card label or filename contains Unicode characters.
- Structured backend events are now ASCII-safe JSON fields and remain backward compatible.

## 0.3.0-beta — 2026-08-21

- Replaced console-style repeated percentages with structured GUI progress events.
- Added overall file-count progress and current-file status.
- Added card capacity/free-space display when reported by the camera.
- Added optional completion notification with downloaded/skipped counts and destination.
- Added a separate camera-transfer-list action for selected files.
- Refined the main transfer card and action hierarchy.
- Documented the settings path and the deliberate no-delete safety policy.

## 0.2.0-beta — 2026-08-16

- Renamed the application to Camera Wi-Fi Transfer and removed model-specific UI text.
- Added automatic Russian/English localization and a manual language selector.
- Added broad JPEG/HEIF, RAW, and video extension support.
- Added optional manual camera IP while keeping automatic PTP/IP discovery.
- Fixed terminal Backspace progress characters appearing as squares in the GUI log.
- Preserved user-selected destination, mode, filters, and date-folder settings.
- Kept native Windows Sony USB pairing without driver replacement.
- Verified PTP/IP discovery and ARW transfer on Sony ILCE-6500.
