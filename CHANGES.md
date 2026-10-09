# Traccar Live (QGIS) — Changes

## v0.2.1

Packaging for the QGIS plugin repository (plugins.qgis.org); no change in behaviour.
- The plugin folder inside the zip is now `traccar_live` (new plugins must use a lowercase, PEP 8
  folder name). If you installed v0.2.0 from its zip, remove the old `Traccar_QGIS` plugin folder
  so the plugin isn't loaded twice.
- Icon (`icon.png`, also on the toolbar button), changelog and category in `metadata.txt`;
  marked experimental for its first release on the plugin repository.
- GPL-2.0-or-later `LICENSE` in the package (required by the repository).

## v0.2.0

Brings the QGIS plugin in line with the QField plugin (Traccar_QField v0.4).

### One time window, two layers
- A single **time window** (Last 15 min … 3 months, custom dates, or a feature's start/end)
  drives the map and both Save buttons — what's shown is what gets saved.
- Layers A / B / C, culling and the Fetch Logs dialog are replaced by two layers:
  **Positions** (latest fix per device, or every fix in the window) and **Tracks**
  (one line per device per save, or keep only the most recent — matched by `device_id`,
  else by the device name field).
- Same fields as the QField template (`traccar_points` / `traccar_tracks`), so one GeoPackage
  works in both. *New GeoPackage…* creates both tables.
- Device name can go into any text field (e.g. `title`); session tag, optionally the feature's
  display value with *From feature*.

### Overlay
- Markers and tracks are two temporary layers, **Temp Markers** and **Temp Tracks**, in a
  **Traccar (live)** group (New GeoPackage names the saved layers *Traccar Positions* and
  *Traccar Tracks*): per-device colours,
  grey when stale, labels, accuracy circles in metres; Identify works. Flagged so QGIS never asks
  to save them; stale copies saved into a project are removed on load.

### Live view: drawn on the map (default) or temporary layers
- **Settings → Advanced → Live view.** Default: drawn straight onto the map canvas — nothing in the
  Layers panel; hover a marker for name, fix age, speed and battery; labels and accuracy circles
  drawn by the plugin. Screen only (not in print layouts).
- Alternative: the temporary layers (Temp Markers / Temp Tracks) — Identify, restyling, print layouts.
- Zoom to a device / Zoom to all work the same in both.

### Unobtrusive
- Main window is a **non-modal pop-up**; close it and Live keeps running. Toolbar: *Traccar Live*
  (open) and *▶ Live* (start/stop). Live status and last error are in the ▶ Live tooltip.
- **Network requests are asynchronous** through QGIS's network manager — QGIS no longer freezes
  while loading, and QGIS proxy settings apply.
- Live errors are shown once per error streak (always logged).

### Fixes
- **Reprojection**: positions and tracks are transformed to the layer CRS (v0.1 wrote
  longitude/latitude into any layer, e.g. Irish Grid layers got points near 0,0).
- Saved features are adapted to the layer type (2D / Z / M / multi) — saving into a 2D line layer
  no longer fails.
- No duplicate history points while a device is idle.
- Layer styling is no longer overwritten on every fetch.
- If the target layer is already being edited, features go into that edit session.
- Settings and Help windows are deleted when closed. Before, every Settings window stayed alive,
  connected to the layer pickers, and closing or switching projects could crash QGIS 3.44.
- Times: shown in local time (incl. summer time); DateTime fields stored as UTC; optional
  `fix_local` / `start_local` / `last_local` text fields.

### Compatibility
- `qgisMinimumVersion=3.28`, **`qgisMaximumVersion=4.99`** — without a maximum, QGIS treats a 3.x
  plugin as "3.99 max" and QGIS 4 refuses to load it.
- Tested (tests/test_plugin.py, 71 checks against a fake Traccar server) on QGIS 3.44.15 LTR
  (Qt 5), 4.2.3 and 4.3-dev (Qt 6): all pass, no Python warnings. Read-only check against
  server.traccar.org (tests/real_server_check.py) in 3.44 and 4.2. Python 3.9 syntax verified for
  QGIS 3.28; 3.28–3.40 not run.
- v0.1 settings migrate automatically (B → Positions, C → Tracks, refresh interval).
