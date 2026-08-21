# Changelog

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
