# Traccar Live (QGIS) — Changes

## v0.4.2 (in development)

- **Areas and lines are drawn while you add them**: the shape (filled for an area), a dot on each corner
  and a dashed line to the mouse. Before, nothing showed until the geofence came back from Traccar
  (the preview was never refreshed).
- **No tick boxes for adding.** Point, Area, Line and Circle are plain menu items: choosing one starts
  it, with a bar over the map saying what to do and a **Cancel** button (or Esc). Once the shape is sent
  — or cancelled — the map tool you had before (e.g. Pan) comes back by itself.
- **A sent geofence stays on the map** while Traccar saves it — marked *sending…* in the Geofences tab
  — instead of vanishing until the reload; Traccar's copy then replaces it (or, if Traccar refused it,
  it goes).
- **A sent point shows at once too**: a dashed ring in the device's colour and "*name* (sending…)",
  until the first refresh or Live update after Traccar accepted it — then the real marker is there
  (in a past time window it simply goes). If Traccar refuses the point, it goes at once.
- Once Traccar accepts a sent point, the plugin **refreshes by itself every 2 s (up to 5 times), Live on
  or off,** until the real device shows. In a time window that ends in the past (custom dates, from
  feature) the point can't appear, so its stand-in goes at once with a note.
- Fixed: the point's details loop reused a variable name, which would have lost track of the marker.
- Tests: the drawing, the dashed line, getting the previous map tool back after sending, Esc and
  Cancel, a sent geofence and a sent point shown at once; 193 checks.

## v0.4.1 — Right-click to send

- **Right-click → Send to Traccar.** Right-click the map (Pan, Identify and other tools with a map menu):
  **Traccar Live → Send point here… / Send circle here…** at the clicked spot, and *Send selected
  features* when the active layer has a selection. In the Layers panel, right-click a vector layer:
  **Send selected features to Traccar…**. Same as the QField plugin's long-press menu.
- **Point, Area, Line and Circle are no longer on the toolbar** — they are under **＋ Add** in the panel
  and in Plugins → Traccar Live. The toolbar keeps *Traccar Live* and *▶ Live*.
- Tested on QGIS 3.44 LTR and 4.2 (180 checks).

## v0.4.0 — Geofences, and adding to Traccar

### Geofences
- Traccar's **geofences** (areas, lines, circles) are drawn on the map with their names, each in **its own
  colour** (Traccar's `color` attribute, else orange). They never go grey like an old fix. *Geofences*
  under Show switches them on or off; they reload with Refresh and Live.
- A **Geofences tab** lists them with type, origin (QGIS / QField or Traccar), description and speed
  limit. Tick to show / hide each one (remembered), double-click to zoom, and ⋯ or right-click to
  **Edit…**, change the **Colour** (ten swatches or any other), **Copy as WKT** or **Delete from Traccar…**
  (always asked first; one made in Traccar gets an extra warning).
- **Edit…** opens the send form filled in: name, description, speed limit, corridor width, calendar,
  colour and a circle's radius. *Save to Traccar* updates the geofence; its shape stays.

### Add to Traccar
- **＋ Add** next to Clear (also in the plugin menu), with the same tools as the
  QField plugin:
  - **📤 Point** — click the map and give a name. A new name adds a device in Traccar (identifier
    `qgis-<name>-<time>`, or your own) and sends the position through Traccar's OsmAnd port; an existing
    device's name moves that device. Points added this way can be deleted again from the device list (⋯).
  - **⬡ Area** / **〰 Line** — click each corner; right-click or Enter to finish, Backspace to undo, Esc to
    start again.
  - **⭕ Circle** — click the centre; radius 250, 500, 1000 m or any other.
  - **📑 Send selected features…** — the active layer's selected features: areas and lines become
    geofences, points become circles or points (devices); name and description from fields. Any CRS,
    multipart, Z / M; holes are dropped and very detailed shapes simplified to fit Traccar's limit.
- **One send form** for every shape, the same as QField's. Geofences: name, description, speed limit
  (km/h; Traccar stores knots), corridor width for lines, Traccar calendar and a colour. New points:
  identifier, category (map icon), group, phone, model and contact. Groups and calendars come from
  your Traccar server.
- A geofence sent with the same name as one sent from QGIS or QField before **replaces** it, so an
  edited sector can be sent again.
- **Settings → Connection → Send point**: the name offered first, and a *Send to* address for servers
  whose OsmAnd port isn't 5055.

### Panel
- **Devices** and **Geofences** tabs, with counts. Right-click a list's column titles to hide columns
  (remembered).

### Also
- README, metadata and Help say clearly that this is **not an official Traccar plugin** and credit
  Traccar (Anton Tananaev and contributors) for all things Traccar.
- Tested on QGIS 3.44 LTR and 4.2 (177 checks).

## v0.3.0

### Dockable panel
- The main window is now a **dock widget**: docked on the right of the map by default; drag it to
  another side, tab it with other panels, or float it. It reopens where it was left.
- The **Traccar Live** toolbar button shows / hides the panel (checked while it is open).

### Per device
- A **tick box** on each device shows or hides it on the map (both live views). Hidden devices are left
  out of *Zoom to all* and the Save buttons, and are remembered between sessions (by device id).
  The list header shows "*n* of *m* shown", and is itself a tick box to show all / hide all.
- **⋯** on each row (or right-click): *Zoom to device*, *Show only this device*, *Show all devices*,
  **Save to file…**, and save just this device's positions / track to the layers (even when hidden).
- **Save to file**: GPX (track with times and altitude + waypoint at the last fix), GeoPackage
  (`traccar_points` + `traccar_tracks`, the shared schema) or CSV (one row per fix).

### Playback
- **Playback** box in the panel: ▶ / ⏸, a time slider over the tracks in the window, ⏮ to the first
  fix, ■ to leave, and a speed from 1× to 1 hour per second.
- Markers move smoothly along the tracks (interpolated between fixes); each track is drawn up to the
  playback time, with the whole track faint underneath in the drawn-on-map view. Temporary layers
  follow playback too. Only ticked devices play; Live updates continue during playback.
- **At:** box to go to a time (to the second); **⌖** pans to the selected device's playback position
  (or all shown devices). Double-click, *Pan to device*, **Copy location** and the *Location* column
  all use the position at the playback time; the *Speed* (interpolated) and *Battery* columns follow it too. Accuracy circles between fixes use the
  larger of the two fixes' accuracy ("the device was somewhere in here").
- Tracks drawn on the map now keep **every fix** (before, tracks over 2000 fixes were thinned, which cut
  corners — and during playback made the route change shape as it played). Each track is reprojected
  once and drawn natively, so long tracks are faster as well as exact.

### Location and clipboard
- **Location** column in the device list (and a line in the marker tooltip), in the format chosen
  under *Location as*: Lat / Long, Project CRS, Custom CRS, Irish Grid (`O 15904 34671`) or
  UK Grid (`NS 45140 72887`) — same grid letters as the QField plugin *Conversion_tools*.
- **Copy location** (⋯ menu): any of those formats, **what3words**, or a WKT point (WGS84 / project CRS).
- **Copy track** (⋯ menu): the fixes in the window as a WKT LineString (WGS84 / project CRS), or one
  line per fix with its local time and location.
- Settings → Advanced → **Location**: lat / long as **decimal degrees, degrees + minutes or degrees +
  minutes + seconds**, with **N/S/E/W or +/−** (also used for a geographic project / custom CRS);
  custom CRS (EPSG:2157 by default) and the what3words API key,
  kept encrypted in QGIS's password manager and sent in a request header; what3words is only
  called when you copy (it needs a plan with *convert-to-3wa*).

The Irish / UK grid letter tables are built from the alphabet rather than written out as 25-letter
strings, which the plugin repository's secrets scan took for possible encoded secrets.

Tests: 151 checks, all pass on QGIS 3.44.15 LTR (Qt 5) and 4.2.3 (Qt 6).

## v0.2.4

No change in behaviour.
- The `.flake8` file is gone: the repository's scanner treats any included tool config as possibly
  suppressing checks ("Validated (configured)", needing an administrator's review). The column
  alignment it allowed has been removed instead, so flake8 is clean with the site's default settings.

## v0.2.3

Code-quality clean-up; no change in behaviour. flake8, pylint (errors), vulture, Bandit and
detect-secrets all report nothing.
- Removed unused imports; restored a collapsed multi-line expression; simpler nearest-marker lookup
  and saved-password fallback.
- `.flake8` in the package documents the deliberate column alignment (E221 / E272) — removed again in 0.2.4.

## v0.2.2

Passes the QGIS plugin repository's security scan (Bandit), which blocked 0.2.1; no change in behaviour.
- The old plain-text password setting is no longer part of the settings defaults; it is read once,
  only to move it into QGIS's password manager.
- The MD5 hash that picks each device's colour is marked as not used for security.

## v0.2.1

Packaging for the QGIS plugin repository (plugins.qgis.org), and the login moves to QGIS's password manager.
- **Login in QGIS's password manager**: the Traccar username and password are kept as a *Basic* login
  (*Traccar Live*) in QGIS's Authentication Manager, encrypted behind the master password; the plugin's
  settings keep only its id. A plain-text password saved by v0.1 / v0.2.0 is moved there the first time
  it is needed and then deleted from the settings. In Settings → Connection the password box stays empty
  — leave it blank to keep the saved password.
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
