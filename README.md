# PiDjiRc2KmzSync

A Raspberry Pi Zero 2 W, battery-powered field relay for syncing DJI
waypoint mission KMZ files between iCloud (via iPad/iPhone/desktop) and
a DJI RC-2 controller, at sites with no WiFi or mains power — only a
phone hotspot.

This project has two parts, kept as sibling folders since they're
different disciplines with different toolchains:

- **[`code/`](code/README.md)** — the Flask-based relay app that runs on
  the Pi, sharing its core sync logic with the desktop app
  [`DJI_RC2_KMZsync`](https://github.com/mpinnuck/DJI_RC2_KMZsync).
- **[`mechanical/`](mechanical/README.md)** — FreeCAD enclosure design
  for the Pi, PowerBoost 1000C, 700mAh LiPo, switch, and connectors.

## Status

- Electronics on order (Pi Zero 2 W, PowerBoost 1000C).
- `code/` now ports real, working logic from `DJI_RC2_KMZsync`'s source
  (not a placeholder scaffold) — see `code/README.md` for what's real vs.
  what still needs bench testing.
- Mechanical design not yet started — waiting on electronics to be
  proven working on the bench first.

See each subfolder's README for details and next steps.
