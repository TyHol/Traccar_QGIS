# Traccar Live for QGIS

A QGIS plugin that shows your [Traccar](https://www.traccar.org) GPS devices on the map for a chosen time window — live — and saves their positions and tracks into your project's layers when you ask it to.

- **One time window for everything** — *Last 15 minutes … Last 3 months*, custom dates, or the start/end of a feature such as an incident. The tracks on the map are the fixes inside the window, and the Save buttons save exactly that.
- **Unobtrusive** — a small window you can close while Live keeps running in the background; QGIS never freezes while loading.
- **Live view drawn on the map** (nothing added to the Layers panel) or as temporary layers when you want Identify, styling or printing.
- **Two layers, saved on demand** — positions and tracks, into any point / line layers, in any CRS.
- **Same layers and fields as the QField plugin [Traccar_QField](https://github.com/TyHol/Traccar_QField)**, so one GeoPackage works in the office and in the field.

Works in **QGIS 3.28 – 4.x** (Qt 5 and Qt 6). Latest release: **[v0.2.0](https://github.com/TyHol/Traccar_QGIS/releases/latest)** · Changes: [CHANGES.md](CHANGES.md)

---

## Install
1. Download **`Traccar_QGIS_v0.2.0.zip`** from the [release page](https://github.com/TyHol/Traccar_QGIS/releases/latest) (don't unzip it).
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**, choose the file, click *Install Plugin*.

You need a Traccar account — your own server or e.g. `https://server.traccar.org`.

## Getting started
1. Click **Traccar Live** on the toolbar, then **Settings… → Connection**: enter the server address (the one you open in a browser), your Traccar email / username and password, and click *Test connection*.
2. Choose a **Time window** and click **▶ Live** to follow devices, or **↻ Refresh** to load the window once.
3. To keep what you see: **Settings… → Layers** → pick a positions layer and a tracks layer — or click **New GeoPackage…** to create both (`traccar.gpkg` in your project folder) — then use **📍 Save positions** / **〰 Save tracks**.

## Using it

### Toolbar and menu
| | |
|---|---|
| **Traccar Live** | opens the main window (time window, devices, save buttons) |
| **▶ Live / ⏹ Live** | starts / stops live updates without opening anything; its tooltip shows the last update and any error |
| Plugins → Traccar Live | the same, plus *Settings…* and *Help* |

You can close the main window at any time — Live keeps running.

### Time window
| Choice | What you get |
|---|---|
| Last 15 min … Last 3 months | Follows the current time. With Live on, new fixes are added and old ones drop off. |
| Custom dates… | From / To pickers in local time, then *Show this window*. |
| From feature… | The start/end of a feature, e.g. an incident: start → end, start + duration, or end − duration. Set the layer up once in Settings → Advanced. |

The device table shows each device's fixes in the window, their time span, speed and battery. **Double-click** a device to centre the map on it; **Zoom to all** fits every device and track. A window that ends in the past cannot change, so Live pauses for it and the markers show each device's last fix in that window.

### Live view
Choose in **Settings… → Advanced → Live view**:

| Drawn on the map (default) | Temporary layers |
|---|---|
| Nothing added to the Layers panel | *Temp Markers* and *Temp Tracks* in a *Traccar (live)* group |
| Hover over a marker for name, fix age, speed and battery | Identify tool, attribute table |
| Plugin's own style | Restyle in Layer Properties |
| Screen only | Shows in print layouts and exports |

Either way: each device has its own colour, grey when its last fix is older than the limit (10 min by default); **Show** switches Markers, Labels, Tracks and Accuracy circles; nothing is written to file and QGIS never asks to save anything.

### Saving
| Button | Option (Settings… → Layers) | Adds |
|---|---|---|
| 📍 Save positions | *Latest fix per device* | one point per device |
| | *Every fix in the time window* | one point per fix |
| 〰 Save tracks | *Add a new track for each device on every save* | one line per device |
| | *Keep only the most recent track per device* | replaces that device's earlier track (matched by `device_id`, or by the device-name field) |

- Points and lines are reprojected to the layer's CRS and adapted to its type (2D / Z / M, single or multi). Tracks carry altitude as Z and the fix time as M where the layer has them.
- If the layer is already being edited, the new features go into that edit session for you to save.

### Fields
Fields are filled **by name** — whichever of these your layer has; others are left alone.

| Positions layer | | Tracks layer | |
|---|---|---|---|
| `device_id` | Traccar device id | `device_id` | Traccar device id |
| `name` | device name | `name` | device name |
| `status` | online / offline | `start_time` | first fix (UTC) |
| `fix_time` | GPS fix time (UTC) | `last_update` | last fix (UTC) |
| `fix_local` | fix time as local text | `start_local`, `last_local` | the same as local text |
| `speed_kmh`, `course` | speed, heading | `from_time`, `to_time` | the time window |
| `altitude_m`, `accuracy_m` | altitude, GPS accuracy | `n_points` | number of fixes |
| `battery`, `motion`, `address` | from the device | `saved_at` | when you saved |
| `fetched_at` | when you saved | | |

Also in Settings:
- **Device name into** — also write the device name into another text field, e.g. `title`.
- **Tag** — stamp a text such as `FIRE-2026-001` into a field of your choice on everything you save; with *From feature*, the feature's display value can be used instead.

### Times
Everything in the plugin is shown in local time, with the right summer/winter offset for each date. Date/time fields are stored in **UTC**, which is how QGIS displays them; for local time use the `*_local` text fields, or in labels and virtual fields `datetime_from_epoch(epoch("fix_time"))`.

## Settings
| Tab | Contains |
|---|---|
| **Connection** | Server URL, email / username, password, *Test connection* |
| **Layers** | Positions layer, device-name field, latest / every fix · Tracks layer, device-name field, add / keep most recent · *New GeoPackage…* |
| **Tag** | On/off, tag text, field, use the feature's display value |
| **Advanced** | Refresh interval (30 s by default), grey-marker limit, live view (on the map / temporary layers), the layer and fields used by *From feature* |

Settings are stored in your QGIS user profile (the password too), so a new computer or QGIS profile needs them entering once. Settings from v0.1 are carried over automatically.

## Troubleshooting
| Message | What to check |
|---|---|
| *Wrong username or password (HTTP 401)* | Settings… → Connection; use the same login as the Traccar web page. |
| *No response — check the server URL* | The address (including `https://`) and your connection / QGIS proxy settings. |
| A device shows *no fixes in window* | Pick a longer window, or check the device's clock. |
| *Could not save to … — see the log* | The layer must be editable and the right kind (points / lines). Details: View → Panels → Log Messages → *Traccar Live*. |

## Development
- The plugin is [`traccar_live.py`](traccar_live.py) (+ `__init__.py`, `metadata.txt`).
- [`tests/test_plugin.py`](tests/test_plugin.py) — 71 checks against a fake Traccar server, run inside QGIS's Python (settings go to a temporary folder, never your QGIS profile):
  ```
  set QT_QPA_PLATFORM=offscreen
  python-qgis-ltr.bat tests\test_plugin.py      (QGIS 3.x)
  python-qgis.bat tests\test_plugin.py          (QGIS 4.x)
  ```
- [`tests/real_server_check.py`](tests/real_server_check.py) — a read-only check against a real Traccar server, using the login saved in a QGIS profile (never printed).
- The release zip contains a `Traccar_QGIS/` folder with the plugin files and `LICENSE`.

## Licence
Copyright © 2026 TyHol. Released under the [GNU General Public License v2.0 or later](LICENSE) (GPL-2.0-or-later), the same licence as QGIS.
