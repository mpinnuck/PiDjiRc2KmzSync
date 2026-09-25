# code/

Flask-based relay app for the Pi. Shares its core sync logic with the
desktop app [`DJI_RC2_KMZsync`](https://github.com/mpinnuck/DJI_RC2_KMZsync)
— most of the files under `backends/`, `model/`, and `services/` are
ported directly from that app's source, since the app was designed with a
single role, single responsibility, the logic turned out to
be platform-independent pure Python.

MTP (via `pymtp`/`libmtp`) is the only connection path this relay uses.

## Motivation

The goal of this project is to make it possible to update a DJI RC-2
mission while working at a remote location. A mission can be exported from
[DJI Mission Planner](https://github.com/mpinnuck/DjiMissionPlanner.git) as a
KMZ file, placed in an iCloud folder, and then
uploaded from an iPad, iPhone, or other browser-connected device to the RC-2
through this Raspberry Pi relay. The modified mission can then be copied
back from the RC-2 to the selected PC/Cloud file when needed.

The field workflow is intentionally simple: the Raspberry Pi provides the
local connection to the RC-2, while the browser device provides the user
interface and access to the iCloud file. The only outside connectivity
required is internet access, typically through a mobile phone hotspot. This
avoids needing a fixed site network or mains-powered computer at the remote
location.

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
- `backends/mtp/mac_mtp_backend.py` — the preserved macOS concrete MTP
  implementation. This is the backend selected when the app runs on macOS.
- `backends/mtp/pi_mtp_backend.py` — an independent clone of the macOS
  implementation for Raspberry Pi Debian/Linux. Pi-specific USB and libmtp
  changes can now be made here without changing the working Mac backend.
- `backends/mtp/linux_mtp_backend.py` — the original Linux implementation
  retained for compatibility while the split backends are validated.
  The Mac and Pi backends use the same `pymtp`/`libmtp` stack, since
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
- `backends/backend_factory.py` — selects `MacMTPBackend` on macOS and
  `PiMTPBackend` on Linux for `mtp:` roots.
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
- `flask_app/` and `client/static/` — the web UI: a three-pane layout
  (RC-2 missions, PC/Cloud KMZ folder tree, and mission preview). Each UUID
  mission row includes its available preview image to make mission
  identification easier; selecting a row keeps it highlighted and shows a
  larger preview with its details. The preview opens in a full-screen
  lightbox when clicked.

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
| 1 | Connect to DJI RC-2 | `MacMTPBackend` on macOS / `PiMTPBackend` on Linux — needs bench testing |
| 2 | Read/write KMZ mission files | `RCBackend` + platform-specific MTP primitives — needs bench testing |
| 3 | Act as WiFi access point | Not part of this app — RaspAP, configured at OS level |
| 4 | Operate in WiFi station mode | Not part of this app — RaspAP, configured at OS level |
| 5 | Browser-compatible UI | Three-pane layout in `client/static/`, with activity logging and preview lightbox |
| 6 | Copy KMZ from iCloud to RC-2, with dummy-slot fallback | `/api/upload` + dummy-slot resolution in `MissionViewModel` |
| 7 | Display RC-2 missions | `/api/missions` — mission list with dummy slot highlighted, plus a small thumbnail per mission (`/api/missions/<guid>/thumbnail`, backed by `RCBackend.get_preview_path` unchanged) and a larger preview panel on the right showing the selected mission's thumbnail, GUID, KMZ name, and any linked PC/Cloud file |
| 8 | Copy KMZ from RC-2 back to iPad/iCloud | `/api/download/<guid>` fetched client-side and written into the file selected in the PC/Cloud folder tree, overwriting it in place (desktop Chrome/Edge, via `FileSystemFileHandle.createWritable()`). Falls back to a plain browser download when no writable handle is available (Safari, or a file picked via the `webkitdirectory` fallback input) |
| 9 | Battery low warning | PowerBoost 1000C's LBO pin wired to a Pi GPIO input (see below); `/api/status`'s `low_battery` field drives an amber badge in the header |

## PC/Cloud folder browsing (right pane)

Uses the File System Access API (`showDirectoryPicker`, requested with
`{ mode: "readwrite" }` since "Copy RC-2 mission → selected PC file"
needs to overwrite a file in place, not just read it) where supported
(desktop Chrome/Edge), persisting the chosen folder's handle in
IndexedDB so it survives across sessions without re-prompting (subject
to the browser re-confirming permission). Safari (iPad/iPhone) doesn't
support this API at all, so there the folder must be re-chosen each
session via a plain `<input type="file" webkitdirectory>` picker.

That fallback still renders a real expandable tree, not a flat list:
`webkitdirectory` gives each file a `webkitRelativePath` string (e.g.
`Air3s/Home/TestMission.kmz`), and `buildFolderTreeFromFiles()` in
`app.js` groups those paths into the same nested folder/file structure
the real API's `buildTreeForDirHandle()` produces — pure string
splitting, no filesystem API involved, so it renders identically on
iPad Safari as on desktop. What the fallback still can't do is persist
the choice across sessions or serve as a **Copy RC-2 → PC** overwrite
target, since neither is possible without a real `FileSystemFileHandle`
— the UI notes both limitations when they apply.

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

## Battery low warning (LBO)

`services/battery_monitor_service.py` reads the PowerBoost 1000C's LBO
(Low Battery Output) pin via a Pi GPIO input, so the web UI can show a
low-battery warning without needing to poll the battery voltage itself.

**Wiring — not a direct connection.** Per Adafruit's own documentation,
LBO is actively pulled to BAT voltage (up to ~4.2V) when the battery is
fine, not floating, and is explicitly "not suitable for direct
connection to 3.3V logic GPIO." The actual circuit, verified against
Adafruit's own forum guidance for this exact pin:
- A 100k ohm resistor from the Pi's 3.3V pin to the GPIO input (BCM
  GPIO4 / physical pin 7 — the `LBO_GPIO_PIN` constant in
  `battery_monitor_service.py`; a fixed hardware wiring choice, not a
  runtime setting).
- A diode in series between that same GPIO node and LBO, anode toward
  the GPIO/resistor node, cathode toward LBO.

When LBO is high (battery fine), the diode is reverse-biased and blocks
it — the GPIO pin only ever sees the safe 3.3V from its own resistor.
When LBO goes low (battery low), the diode conducts and lets LBO pull
the GPIO node down, which reads as a low input.

**Software.** Uses `gpiozero` (the current Raspberry Pi Foundation
recommendation) rather than the older `RPi.GPIO`, since `RPi.GPIO`'s
direct `/dev/mem` access doesn't work reliably on Debian Trixie's
gpiochip character-device model. Degrades gracefully rather than
crashing when there's no real GPIO backend available — e.g. running the
dev server on a Mac, or before `python3-lgpio` is installed on the Pi —
in which case `/api/status`'s `low_battery` field is `null` (unknown),
not `false` (battery fine), and the UI logs a one-time warning instead
of showing a false "all clear."

**On the Pi (not needed for Mac-side dev):**
```
sudo apt install python3-lgpio
```

**UI**: an amber "⚠ Battery low" badge appears next to the RC-2/Pi
connection status pills in the header, only when `low_battery === true`
— hidden otherwise, including the "unknown" case.

## Production deployment (systemd + gunicorn)

The relay runs in production as `pidjirc2kmzsync.service`, a user-level
systemd unit (see that file's own comments for the one-time setup:
install the unit, `systemctl --user enable --now`, and `sudo loginctl
enable-linger mark` once so it starts at boot without an interactive
login). Deliberately user-level rather than system-level, so ongoing
deploys never need sudo.

`deploy.sh` does not restart the service by default. Use
`./deploy.sh --restart` when Python code changes. The equivalent Make
commands are `make deploy RESTART=1` and `make deploy-restart`.
The `--pip` option reinstalls Python requirements when dependencies change.
One thing that isn't automatic: if `pidjirc2kmzsync.service` itself
changes (not just the app code), it needs re-copying to
`~/.config/systemd/user/` and a `systemctl --user daemon-reload` by
hand, since deploy.sh only syncs it into the app folder, not the
systemd unit directory.

## Next steps

1. If read/write operations show MTP session flakiness, tune
  `PiMTPBackend` independently without changing the working
  `MacMTPBackend`.
2. Set up RaspAP for WiFi AP/station-mode switching in the field.

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
- The activity log records actions, warnings, errors, connection status,
  file names, and copy results. It is scrollable and can be copied to the
  clipboard.
- The RC-2 connection status is shown as white text on green when connected
  and white text on red when disconnected or unavailable.

## Running (dev)

```bash
cd code
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Then visit `http://<pi-ip>:8000` from a browser on the same network.

## Make commands

Run these from the `code/` directory:

```bash
make deploy
make deploy RESTART=1
make deploy-restart
make zip
```

`make deploy` syncs updated application files without restarting the service.
Use one of the restart forms after Python changes. `make zip` creates the
source archive from the repository root.
