# code/

Flask-based relay app for the Pi. Shares its core sync logic with the
desktop app [`DJI_RC2_KMZsync`](https://github.com/mpinnuck/DJI_RC2_KMZsync)
— most of the files under `backends/`, `model/`, and `services/` are
ported directly from that app's source, since that logic turned out to
be platform-independent pure Python.

MTP (via `pymtp`/`libmtp`) is the only connection path this relay uses.

## What's real vs. what's new vs. what's a stub

**Ported verbatim (no changes needed — pure Python, no OS-specific calls):**
- `backends/rc_backend.py` — the concrete `RCBackend` base class. All
  mission listing, GUID-slot discovery, copy orchestration, and preview
  caching lives here; it only depends on nine abstract `_raw_*`
  primitives that subclasses implement.
- `backends/unavailable_rc_backend.py`
- `model/rc2_mission.py`, `model/kmz_file.py`
- `services/mtp_date_normalizer.py`, `services/sync_engine.py`,
  `services/copy_map_service.py`, `services/mission_verification_service.py`

**New for this relay — real, substantive port (not a stub), but needs bench testing:**
- `backends/mtp/linux_mtp_backend.py` — ported from the desktop app's
  `MacMTPBackend`. Both use the same `pymtp`/`libmtp` stack, since
  libmtp is a cross-platform native library, so nearly all of the
  session management, folder/file tree traversal and caching, and the
  chunked `GetPartialObject` read logic (RC-2 blocks plain MTP
  `GetObject`, so this workaround is essential) carried over unchanged.
  What's genuinely different on Linux vs macOS:
    - `_preload_libmtp()`: searches Debian/Raspberry Pi OS library
      paths instead of Homebrew paths.
    - Dropped a macOS-only USB device-info tool (`ioreg`), replaced with
      Linux process-name matching (`pgrep`) for the same purpose — see
      `_release_host_mtp_hold()`.
    - `_raw_read_file()`'s retry logic is simplified to one
      reconnect-and-retry cycle rather than the Mac version's several
      nested retry passes (tuned from real-world macOS session
      flakiness). If the Pi turns up similar flakiness, port more of
      that retry structure back in — see the file's docstring.
  **This has not been tested against real RC-2 hardware yet** — the
  logic is a faithful, reasoned port of working code, not a stub, but
  treat it as unverified until bench-tested with a Pi + RC-2 over USB-C.
- `backends/backend_factory.py` — adapted: Linux-only, `mtp:` roots
  only.
- `config/config_manager.py` — adapted: keeps `rc2_folder` and
  `dummy_slot_guid` (server-side, since they describe the RC-2 device
  itself), drops `pc_folder` entirely. The "PC/Cloud" folder is owned
  by whichever browser is connected (iPad/iPhone/desktop), not the Pi,
  so it lives in the browser's own persistent storage instead — see
  `client/static/app.js`.
- `viewmodel/mission_viewmodel.py` — adapted from the desktop app's
  `SyncViewModel` for a request/response (not GUI-event) style, and
  implements the dummy-slot resolution: if a specific RC-2 mission is
  selected, copy there; otherwise fall back to the configured dummy
  mission slot. Filename resolution (existing KMZ name in a non-empty
  slot, or `<slot GUID>.kmz` for an empty slot) reuses
  `SyncEngine.resolve_destination_filename` unchanged, and MTP copy
  verification reuses `MissionVerificationService` unchanged.
- `flask_app/` and `client/static/` — the web UI: a two-pane layout
  (RC-2 missions on the left, PC/Cloud KMZ folder tree on the right),
  matching the requirements below. Each UUID mission row includes its
  available preview image to make mission identification easier; selecting
  a row keeps it highlighted and shows a larger preview with its details.

## Dummy-slot copy behavior (as implemented)

Ported from the desktop app's exact `_on_copy` / `execute_copy` logic:

- **RC-2 mission selected on the left pane** → the chosen PC/Cloud KMZ
  is copied into that mission's GUID folder, renamed to the slot's
  existing KMZ filename (if it already holds one) or to
  `<mission GUID>.kmz` if the slot is empty.
- **No RC-2 mission selected** → the KMZ is copied into the configured
  **dummy mission slot** instead (an existing RC-2 GUID folder you
  designate via "Set selected RC-2 mission as dummy" in the UI — new
  folders can't be created on the RC-2, so this must be one of its
  existing slots), with the same filename-resolution rule.

## Requirements covered

| # | Requirement | Status |
|---|---|---|
| 1 | Connect to DJI RC-2 | `LinuxMTPBackend` — real port, needs bench testing |
| 2 | Read/write KMZ mission files | `RCBackend` + `LinuxMTPBackend` primitives — real port, needs bench testing |
| 3 | Act as WiFi access point | Not part of this app — RaspAP, configured at OS level |
| 4 | Operate in WiFi station mode | Not part of this app — RaspAP, configured at OS level |
| 5 | Browser-compatible UI | Two-pane layout in `client/static/` |
| 6 | Copy KMZ from iCloud to RC-2, with dummy-slot fallback | `/api/upload` + dummy-slot resolution in `MissionViewModel` |
| 7 | Display RC-2 missions | `/api/missions` — mission list with dummy slot highlighted, plus a small thumbnail per mission (`/api/missions/<guid>/thumbnail`, backed by `RCBackend.get_preview_path` unchanged) and a larger preview panel on the right showing the selected mission's thumbnail, GUID, KMZ name, and any linked PC/Cloud file |
| 8 | Copy KMZ from RC-2 back to iPad/iCloud | `/api/download/<guid>` fetched client-side and written into the file selected in the PC/Cloud folder tree, overwriting it in place (desktop Chrome/Edge, via `FileSystemFileHandle.createWritable()`). Falls back to a plain browser download when no writable handle is available (Safari, or a file picked via the `webkitdirectory` fallback input) |

## PC/Cloud folder browsing (right pane)

Uses the File System Access API (`showDirectoryPicker`, requested with
`{ mode: "readwrite" }` since "Copy RC-2 mission → selected PC file"
needs to overwrite a file in place, not just read it) where supported
(desktop Chrome/Edge), persisting the chosen folder's handle in
IndexedDB so it survives across sessions without re-prompting (subject
to the browser re-confirming permission). Safari (iPad/iPhone) doesn't
support this API, so there the folder must be re-chosen each session
via a plain `<input type="file" webkitdirectory>` picker — this
fallback can supply an upload source but can't be an overwrite target,
since it yields no `FileSystemFileHandle`; the UI notes both
limitations when they apply.

## Mission thumbnails and mission <-> PC file associations

- **Thumbnails**: each mission in the left pane, and the selected
  mission in the right-hand preview panel, fetch
  `/api/missions/<guid>/thumbnail`, which calls
  `RCBackend.get_preview_path()` unchanged from the desktop app — it
  already handles the RC-2's `map_preview` folder layout (a flat
  `<guid>.jpg`, or a nested `<guid>/` subfolder) and caches the result
  locally. Missions with no preview image just show no thumbnail
  (`<img>` `onerror` hides it) rather than an error.
- **Associate**: the "Associate mission ↔ PC file" button links a
  selected RC-2 mission to a selected PC/Cloud file *without copying
  any data* — it calls `MissionViewModel.associate_pc_file()`, which
  writes into the same `CopyMapService` history the desktop app uses
  after a real copy, just without the `RCBackend` transfer step. This
  is for cases where you want to remember "this PC file is the source
  for this mission" (e.g. after copying it in some other way) without
  re-transferring it. `/api/copy-map` returns the current associations
  so the UI can show them next to each mission and in the preview
  panel.

## Next steps

1. **Bench-test `LinuxMTPBackend` against a real RC-2** once the Pi and
   USB-C cabling are in hand. This is the critical unknown: confirm
   `pymtp`/`libmtp` install cleanly on Raspberry Pi OS
   (`sudo apt install libmtp-dev && pip install pymtp`), that the RC-2
   enumerates over USB without any developer/debug mode needed, and
   that the `GetPartialObject` chunked-read workaround behaves the same
   as it does on macOS.
2. If read/write operations show the same session flakiness the Mac
   version needed extra retries for, port more of
   `MacMTPBackend._raw_read_file`'s nested retry logic back in (see
   `linux_mtp_backend.py`'s module docstring for what was simplified).
3. Flash Raspberry Pi OS Lite, install `libmtp-dev`/`pymtp`, set up
   RaspAP, deploy this app, wire up as a systemd service running
   gunicorn (single worker — only one process should hold the MTP/USB
   session at a time).

## Current development notes

- Mac testing is supported by the OS-sensitive MTP backend. On macOS, it
  detects and releases known Apple camera processes that can claim the RC-2
  USB interface before `libmtp` connects. On Raspberry Pi, the backend uses
  the Linux process checks instead.
- Flask development mode runs with the reloader disabled so only one process
  owns the RC-2 MTP session.
- Selecting an RC-2 mission does not reload or blank the mission list. The
  selected UUID remains highlighted while its larger preview is displayed.
- Associating a mission with a PC/Cloud KMZ refreshes the linked metadata
  without clearing the mission list or losing the current selection.

## Running (dev)

```bash
cd code
pip install -r requirements.txt
python run.py
```

Then visit `http://<pi-ip>:8000` from a browser on the same network.
