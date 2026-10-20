# Copyright (C) 2026 TyHol
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Traccar Live – QGIS Plugin  v0.4.2

Follows Traccar devices on the QGIS map within one chosen time window, and saves
their positions and tracks to two project layers on demand. Mirrors the QField
plugin Traccar_QField v0.7.2, and uses the same layer schema, so one GeoPackage
works in both.

  Time window  — Last 15 min … 3 months, custom dates, or a feature's start/end.
                 The tracks shown are the fixes inside the window; the Save
                 buttons write exactly that.
  Overlay      — two temporary layers, "Temp Markers" and "Temp Tracks", in a
                 "Traccar (live)" group. Never written to file.
  Live         — refreshes in the background; a "Last …" window keeps moving.
  Save         — positions layer (latest fix, or every fix in the window) and
                 tracks layer (add a new line per device, or keep only the most
                 recent).
  Per device   — tick to show / hide on the map; save one device to a GPX,
                 GeoPackage or CSV file, or to the layers; copy its location
                 (lat/long, project / custom CRS, Irish / UK grid, what3words,
                 WKT) or its track (WKT) to the clipboard.
  Playback     — replay the tracks in the window: markers move along them.

Unobtrusive by design: the main panel is a dock widget (docked, tabbed or
floating) that can be closed while Live runs (toolbar button ▶ Live starts/stops it), network requests are
asynchronous, and Live errors are reported once per error streak.

Compatible with QGIS 3.28 LTR … 3.44 (Qt5 / PyQt5) and QGIS 4.x (Qt6 / PyQt6).
"""

import csv
import json
import math
import os
import base64
import bisect
import hashlib
import re
import string
import time
from html import escape as _xml
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as _quote

from qgis.PyQt.QtCore import QTimer, QSettings, QDateTime, QDate, QTime, QUrl, Qt, QPointF
from qgis.PyQt.QtGui import (
    QColor, QCursor, QIcon, QPainter, QPen, QBrush, QFont, QPolygonF, QPainterPath, QTransform, QPixmap,
)
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDateTimeEdit, QComboBox, QCheckBox,
    QRadioButton, QButtonGroup, QPushButton, QDialogButtonBox, QTabWidget,
    QWidget, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog, QMessageBox, QTextBrowser, QToolButton, QToolTip,
    QMenu, QScrollArea, QFrame, QSlider, QInputDialog, QColorDialog, QCompleter,
)
from qgis.core import (
    Qgis, QgsApplication, QgsAuthMethodConfig, QgsProject, QgsVectorLayer, QgsFeature,
    QgsFeatureRequest, QgsGeometry, QgsPoint, QgsPointXY, QgsLineString,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsRectangle, QgsCsException,
    QgsVectorFileWriter, QgsVectorLayerUtils, QgsWkbTypes, QgsMessageLog,
    QgsNetworkAccessManager, QgsMapLayerProxyModel, QgsFieldProxyModel,
    QgsMarkerSymbol, QgsLineSymbol, QgsSimpleMarkerSymbolLayer, QgsSymbolLayer,
    QgsSingleSymbolRenderer, QgsProperty, QgsUnitTypes,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
)
from qgis.gui import (
    QgsMapLayerComboBox, QgsFieldComboBox, QgsMapCanvasItem, QgsDockWidget, QgsProjectionSelectionWidget,
    QgsMapToolEmitPoint, QgsMapTool, QgsRubberBand,
)

try:
    from qgis.core import NULL
except ImportError:          # pragma: no cover
    NULL = None


# ════════════════════════════════════════════════════════════════════════════
#  QGIS 3 / 4  (PyQt5 / PyQt6) compatibility
# ════════════════════════════════════════════════════════════════════════════

def _e(root, *chain):
    """Resolve root.a.b (PyQt6 scoped enums) with fallback to root.b (PyQt5)."""
    try:
        obj = root
        for attr in chain:
            obj = getattr(obj, attr)
        return obj
    except AttributeError:
        return getattr(root, chain[-1])


def _new_or_old(new_root, new_name, old_root, old_name):
    """A QGIS enum that moved (e.g. QgsWkbTypes.PointGeometry → Qgis.GeometryType.Point)."""
    try:
        return getattr(new_root, new_name)
    except AttributeError:
        return getattr(old_root, old_name)


_GEOM_POINT = _new_or_old(getattr(Qgis, "GeometryType", None), "Point", QgsWkbTypes, "PointGeometry")
_GEOM_LINE = _new_or_old(getattr(Qgis, "GeometryType", None), "Line",  QgsWkbTypes, "LineGeometry")
_GEOM_POLY = _new_or_old(getattr(Qgis, "GeometryType", None), "Polygon", QgsWkbTypes, "PolygonGeometry")
_LAYER_VECTOR = _new_or_old(getattr(Qgis, "LayerType", None), "Vector",
                            getattr(__import__("qgis.core", fromlist=["core"]), "QgsMapLayerType", None), "VectorLayer")
_LF_POINT = _new_or_old(getattr(Qgis, "LayerFilter", None), "PointLayer",
                        getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "PointLayer")
_LF_LINE = _new_or_old(getattr(Qgis, "LayerFilter", None), "LineLayer",
                       getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "LineLayer")
_LF_VECTOR = _new_or_old(getattr(Qgis, "LayerFilter", None), "VectorLayer",
                         getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "VectorLayer")
_FF_STRING = _e(QgsFieldProxyModel, "Filter", "String")
_RENDER_M = _new_or_old(getattr(Qgis, "RenderUnit", None), "MetersInMapUnits",
                        QgsUnitTypes, "RenderMetersInMapUnits")
_MSG_INFO = _e(Qgis, "MessageLevel", "Info")
_MSG_WARN = _e(Qgis, "MessageLevel", "Warning")
_MSG_OK = _e(Qgis, "MessageLevel", "Success")
_HTTP_STATUS = _e(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
_NET_OK = _e(QNetworkReply, "NetworkError", "NoError")
_BTN_OK = _e(QDialogButtonBox, "StandardButton", "Ok")
_BTN_CANCEL = _e(QDialogButtonBox, "StandardButton", "Cancel")
_DLG_OK = _e(QDialog, "DialogCode", "Accepted")
_ECHO_PWD = _e(QLineEdit, "EchoMode", "Password")
_NO_EDIT = _e(QAbstractItemView, "EditTrigger", "NoEditTriggers")
_SEL_ROWS = _e(QAbstractItemView, "SelectionBehavior", "SelectRows")
_HV_STRETCH = _e(QHeaderView, "ResizeMode", "Stretch")
_HV_CONTENTS = _e(QHeaderView, "ResizeMode", "ResizeToContents")
_VFW_OK = _e(QgsVectorFileWriter, "WriterError", "NoError")
_GPKG_NEW = _e(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
_GPKG_LAYER = _e(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
_ANTIALIAS = _e(QPainter, "RenderHint", "Antialiasing")
_CAP_ROUND = _e(Qt, "PenCapStyle", "RoundCap")
_JOIN_ROUND = _e(Qt, "PenJoinStyle", "RoundJoin")
_NO_BRUSH = _e(Qt, "BrushStyle", "NoBrush")
_CHECKED = _e(Qt, "CheckState", "Checked")
_UNCHECKED = _e(Qt, "CheckState", "Unchecked")
_PARTIAL = _e(Qt, "CheckState", "PartiallyChecked")
_ITEM_CHECKABLE = _e(Qt, "ItemFlag", "ItemIsUserCheckable")
_CUSTOM_MENU = _e(Qt, "ContextMenuPolicy", "CustomContextMenu")
_NO_FRAME = _e(QFrame, "Shape", "NoFrame")
DOCK_AREAS = {"left": _e(Qt, "DockWidgetArea", "LeftDockWidgetArea"),
              "right": _e(Qt, "DockWidgetArea", "RightDockWidgetArea"),
              "top": _e(Qt, "DockWidgetArea", "TopDockWidgetArea"),
              "bottom": _e(Qt, "DockWidgetArea", "BottomDockWidgetArea")}


def _sl_prop(name):
    """Symbol-layer data-defined property: QgsSymbolLayer.Property.X (3.30+) or .PropertyX."""
    prop = getattr(QgsSymbolLayer, "Property", None)
    if prop is not None and hasattr(prop, name):
        return getattr(prop, name)
    return getattr(QgsSymbolLayer, "Property" + name)


def _is_null(v):
    return v is None or (NULL is not None and v == NULL)


# ════════════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════════════

SETTINGS_NS = "TraccarLive"
MENU_LABEL = "&Traccar Live"
DEFAULT_URL = "https://server.traccar.org"
GROUP_NAME = "Traccar (live)"
OVERLAY_PROP = "traccar_overlay"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

# Time window choices: minutes > 0 = moving "Last …", -1 = custom dates, -2 = from feature
# Location formats shown in the device list and offered by Copy location
COORD_FORMATS = [("latlon", "Lat / Long"), ("project", "Project CRS"), ("custom", "Custom CRS"),
                 ("ig", "Irish Grid"), ("uk", "UK Grid")]
W3W_URL = "https://api.what3words.com/v3"
W3W_KEY_SETTING = "TraccarLive/w3w_key"     # in QGIS's password manager (encrypted)
DEVICE_COLUMNS = ("Device", "Location", "Fixes", "Time span", "Speed", "Battery", "")
GEOFENCE_COLUMNS = ("Geofence", "Type", "From", "Description", "Speed limit", "")
_MENU_COL = DEVICE_COLUMNS.index("")       # the ⋯ column of the device list
_GF_MENU_COL = GEOFENCE_COLUMNS.index("")  # the ⋯ column of the geofence list
# Playback speed: seconds of track per real second
PLAY_SPEEDS = [("1×", 1), ("10×", 10), ("1 min / s", 60), ("5 min / s", 300),
               ("15 min / s", 900), ("1 h / s", 3600)]

WINDOW_CHOICES = [
    ("Last 15 minutes", 15), ("Last 30 minutes", 30), ("Last 1 hour", 60),
    ("Last 2 hours", 120), ("Last 3 hours", 180), ("Last 6 hours", 360),
    ("Last 12 hours", 720), ("Last 1 day", 1440), ("Last 3 days", 4320),
    ("Last 1 week", 10080), ("Last 2 weeks", 20160), ("Last 1 month", 43200),
    ("Last 3 months", 129600), ("Custom dates…", -1), ("From feature…", -2),
]

# Layer schema shared with Traccar_QField (tools/make_template_gpkg.py there).
# (name, memory-provider type)
POINT_SCHEMA = [
    ("device_id", "integer"), ("name", "string(80)"), ("status", "string(20)"),
    ("fix_time", "datetime"), ("fix_local", "string(40)"), ("speed_kmh", "double"),
    ("course", "double"), ("altitude_m", "double"), ("accuracy_m", "double"),
    ("battery", "double"), ("address", "string(255)"), ("motion", "string(10)"),
    ("fetched_at", "datetime"), ("tag", "string(80)"),
]
TRACK_SCHEMA = [
    ("device_id", "integer"), ("name", "string(80)"), ("start_time", "datetime"),
    ("last_update", "datetime"), ("start_local", "string(40)"), ("last_local", "string(40)"),
    ("from_time", "datetime"), ("to_time", "datetime"), ("n_points", "integer"),
    ("saved_at", "datetime"), ("tag", "string(80)"),
]

DEFAULTS = {
    "server_url": DEFAULT_URL, "username": "",
    "authcfg": "",                  # id of the login in QGIS's Authentication Manager
    "window_minutes": 60, "custom_from": "", "custom_to": "",
    "event_layer_id": "", "event_display_field": "", "event_start_field": "",
    "event_end_field": "", "event_feature_fid": -1, "feature_span": 0, "feature_duration": 120,
    "live_interval_s": 30, "stale_minutes": 10,
    "show_markers": True, "show_labels": True, "show_tracks": True, "show_accuracy": False,
    "points_layer_id": "", "points_name_field": "", "points_mode": 0,
    "tracks_layer_id": "", "tracks_name_field": "", "tracks_mode": 0,
    "tag_enabled": False, "tag_text": "", "tag_field": "", "tag_from_feature": False,
    "live_view": "canvas",          # "canvas" = drawn on the map, "layers" = Temp Markers / Temp Tracks
    "hidden_devices": "",           # comma-separated Traccar device ids not shown (nor saved by the buttons)
    "dock_area": "right", "dock_floating": False,
    "coord_format": "latlon",       # Location column: one of COORD_FORMATS
    "custom_crs": "EPSG:2157",      # "Custom CRS" location format
    "latlon_style": "dd",           # lat / long as "dd" decimal degrees, "dm" deg + min, "dms" deg + min + sec
    "latlon_hemi": "signs",         # "signs" (-6.31) or "letters" (6.31 W)
    "play_speed": 60,               # playback: seconds of track per real second
    "send_device_id": "",           # Send point: name offered first when a point is clicked
    "send_url": "",                 # Send point: OsmAnd endpoint; blank = http://<server host>:5055
    "show_geofences": True,         # Traccar geofences drawn on the map
    "circle_radius": 500,           # ⭕ Circle / points sent as geofences: radius in metres
    "geofence_color": "#E65100",    # colour given to geofences sent from QGIS (Traccar's 'color' attribute)
    "hidden_geofences": "",         # comma-separated Traccar geofence ids not drawn on the map
    "device_columns_hidden": "",    # column titles hidden in the device list ("Fixes,Battery")
    "geofence_columns_hidden": "",  # … and in the geofence list
    "v2_migrated": False,
}


# ════════════════════════════════════════════════════════════════════════════
#  Time helpers
#  Traccar API times are ISO 8601 UTC ("2026-10-09T08:15:30.000+00:00").
#  Everything shown to the user is local time (incl. summer time for that date);
#  DateTime fields are written as UTC; *_local text fields hold local wall-clock.
# ════════════════════════════════════════════════════════════════════════════

_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_utc(s):
    """Traccar ISO string → aware UTC datetime, or None. Naive strings are UTC."""
    if not s:
        return None
    s = _TZ_NO_COLON.sub(r"\1:\2", str(s).strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:19])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_local(s):
    """'YYYY-MM-DD HH:MM' (or with offset) entered by the user → aware UTC datetime."""
    if not s:
        return None
    s = _TZ_NO_COLON.sub(r"\1:\2", str(s).strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if len(s) > 10 else s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive → local time zone (DST-aware)
    return dt.astimezone(timezone.utc)


def _attr_to_utc(v):
    """Event-layer attribute (QDateTime, QDate, datetime or text) → aware UTC datetime."""
    if _is_null(v):
        return None
    if isinstance(v, QDateTime):
        return datetime.fromtimestamp(v.toMSecsSinceEpoch() / 1000, timezone.utc) if v.isValid() else None
    if isinstance(v, QDate):
        return datetime(v.year(), v.month(), v.day()).astimezone().astimezone(timezone.utc) if v.isValid() else None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.astimezone()).astimezone(timezone.utc)
    return _parse_local(str(v))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        "%03dZ" % (dt.microsecond // 1000)


def _qdt(dt):
    """aware datetime → QDateTime in UTC."""
    return QDateTime.fromMSecsSinceEpoch(int(dt.timestamp() * 1000)).toUTC()


def _speed_text(pos):
    """Traccar speed (knots) → '12 km/h'; '' without a position."""
    return ("%d km/h" % round((pos.get("speed") or 0) * 1.852)) if pos else ""


def _battery_text(pos):
    bat = ((pos or {}).get("attributes") or {}).get("batteryLevel")
    return ("%g%%" % float(bat)) if isinstance(bat, (int, float)) else ""


def _qdt_local(dt):
    """aware datetime → QDateTime in local time (for date/time pickers)."""
    return QDateTime.fromMSecsSinceEpoch(int(dt.timestamp() * 1000))


def _fmt_local(dt):
    return dt.astimezone().strftime("%d %b %H:%M") if dt else "—"


def _local_text(dt):
    """'2026-10-09 09:15:30 UTC+01:00' — local wall-clock time with its offset."""
    if not dt:
        return None
    loc = dt.astimezone()
    off = loc.strftime("%z")
    return loc.strftime("%Y-%m-%d %H:%M:%S") + " UTC" + off[:3] + ":" + off[3:]


def _span_text(a, b):
    """'09:09 – 09:22' for today, otherwise with dates. Local time."""
    if not a or not b:
        return "—"
    la, lb = a.astimezone(), b.astimezone()
    today = datetime.now().astimezone().date()
    if la.date() == lb.date() == today:
        return la.strftime("%H:%M") + " – " + lb.strftime("%H:%M")
    if la.date() == lb.date():
        return la.strftime("%d %b %H:%M") + " – " + lb.strftime("%H:%M")
    return la.strftime("%d %b %H:%M") + " – " + lb.strftime("%d %b %H:%M")


def _age_text(dt):
    if not dt:
        return "?"
    s = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min" % (s // 60)
    if s < 86400:
        return "%d h" % (s // 3600)
    return "%d d" % (s // 86400)


# ════════════════════════════════════════════════════════════════════════════
#  Grid references (same letters and layout as the QField plugin Conversion_tools)
# ════════════════════════════════════════════════════════════════════════════

# The 25 letters A–Z without I, as a 5 × 5 square from the north-west (A) to the south-east (Z).
# Built, not written out: a literal 25-letter string trips secret scanners' entropy check.
_OS_LETTERS = string.ascii_uppercase.replace("I", "")             # Ordnance Survey letters
_IG_LETTERS = "".join(_OS_LETTERS[i:i + 5] for i in range(20, -1, -5))   # Irish Grid, rows from the south


LATLON_STYLES = [("dd", "Decimal degrees  (53.365000)"),
                 ("dm", "Degrees, minutes  (53° 21.900')"),
                 ("dms", "Degrees, minutes, seconds  (53° 21' 54.00\")")]
LATLON_HEMI = [("signs", "+ / −  (53.365000, -6.310000)"),
               ("letters", "N / S / E / W  (53.365000 N, 6.310000 W)")]


def _coord(value, hemispheres, style, letters):
    """One latitude or longitude: decimal degrees ('dd'), degrees + minutes ('dm') or
    + seconds ('dms'); with N/S/E/W (letters) or a minus sign for south / west."""
    a = abs(value)
    if style == "dm":
        total = round(a * 60, 3)                          # rounded first: never shows 60.000'
        deg = int(total // 60)
        text = "%d° %06.3f'" % (deg, total - deg * 60)
    elif style == "dms":
        total = round(a * 3600, 2)
        deg = int(total // 3600)
        mins = int((total - deg * 3600) // 60)
        text = "%d° %02d' %05.2f\"" % (deg, mins, total - deg * 3600 - mins * 60)
    else:
        text = "%.6f" % a
    if letters:
        return "%s %s" % (text, hemispheres[0] if value >= 0 else hemispheres[1])
    nonzero = any(ch in "123456789" for ch in text)       # no "-0.000000"
    return ("-" if value < 0 and nonzero else "") + text


def format_latlon(lat, lon, style="dd", hemi="signs"):
    """'lat, lon' — e.g. '53.365000, -6.310000', "53° 21.900' N, 6° 18.600' W",
    '53° 21\' 54.00" N, 6° 18\' 36.00" W'."""
    letters = hemi == "letters"
    return "%s, %s" % (_coord(lat, "NS", style, letters), _coord(lon, "EW", style, letters))


def irish_grid_ref(x, y):
    """Irish Grid (EPSG:29903) easting / northing → 'H 54321 89797', or '' outside the grid."""
    x, y = int(round(x)), int(round(y))
    if not (0 <= x < 500000 and 0 <= y < 500000):
        return ""
    return "%s %05d %05d" % (_IG_LETTERS[(y // 100000) * 5 + x // 100000], x % 100000, y % 100000)


def uk_grid_ref(x, y):
    """British National Grid (EPSG:27700) easting / northing → 'NS 45140 72887', or '' outside."""
    x, y = int(round(x)), int(round(y))
    if not (0 <= x < 700000 and 0 <= y < 1300000):
        return ""
    e, n = x // 100000, y // 100000
    first = (19 - n) - (19 - n) % 5 + (e + 10) // 5        # 500 km square
    second = (19 - n) * 5 % 25 + e % 5                     # 100 km square within it
    return "%s%s %05d %05d" % (_OS_LETTERS[first], _OS_LETTERS[second], x % 100000, y % 100000)


def _device_color(name):
    """Deterministic colour from the device name — same name, same colour."""
    # MD5 only spreads names over the colour wheel — not used for security
    hue = int(hashlib.md5(str(name).encode(), usedforsecurity=False).hexdigest()[:4], 16) % 360
    return QColor.fromHsv(hue, 190, 200).name()


# ════════════════════════════════════════════════════════════════════════════
#  Login storage — QGIS Authentication Manager (encrypted, behind QGIS's master
#  password). The plugin's settings keep only the config id, never the password.
# ════════════════════════════════════════════════════════════════════════════

AUTH_NAME = "Traccar Live"


def _auth_result(res, cfg):
    """storeAuthenticationConfig / loadAuthenticationConfig return (ok, config) in
    QGIS 3.44 / 4.x and a plain bool (config filled in place) in older builds."""
    return res if isinstance(res, tuple) else (res, cfg)


def load_login(authcfg):
    """(username, password) stored under authcfg, or None."""
    if not authcfg:
        return None
    cfg = QgsAuthMethodConfig()
    ok, cfg = _auth_result(QgsApplication.authManager().loadAuthenticationConfig(authcfg, cfg, True), cfg)
    if not ok or not cfg.id():
        return None
    return cfg.config("username"), cfg.config("password")


def store_login(authcfg, url, username, password):
    """Create or update the plugin's Basic login. Returns its id, or '' if QGIS
    could not store it (e.g. the master password prompt was cancelled)."""
    mgr = QgsApplication.authManager()
    cfg = QgsAuthMethodConfig()
    exists = False
    if authcfg:
        exists, cfg = _auth_result(mgr.loadAuthenticationConfig(authcfg, cfg, True), cfg)
        exists = exists and bool(cfg.id())
    if not exists:
        cfg = QgsAuthMethodConfig("Basic")
        cfg.setName(AUTH_NAME)
    cfg.setMethod("Basic")
    cfg.setUri(url)
    cfg.setConfig("username", username)
    cfg.setConfig("password", password)
    cfg.setConfig("realm", "")
    if exists:
        return cfg.id() if mgr.updateAuthenticationConfig(cfg) else ""
    ok, cfg = _auth_result(mgr.storeAuthenticationConfig(cfg), cfg)
    return cfg.id() if ok else ""


# ════════════════════════════════════════════════════════════════════════════
#  Asynchronous Traccar API client (QGIS network manager → proxy settings apply,
#  QGIS never freezes). Accept: application/json stops Traccar sending a
#  WWW-Authenticate challenge, so a wrong password never pops up a login box.
# ════════════════════════════════════════════════════════════════════════════

class _Api:
    """creds() → (username, password) or None (no login saved)."""

    def __init__(self, cfg, creds):
        self.cfg = cfg
        self.creds = creds
        self._pending = set()

    def get(self, path, on_ok, on_err):
        self._request("GET", path, None, on_ok, on_err)

    def post(self, path, obj, on_ok, on_err):
        """POST obj as JSON (e.g. create a device); errors include Traccar's reason."""
        self._request("POST", path, json.dumps(obj).encode("utf-8"), on_ok, on_err)

    def put(self, path, obj, on_ok, on_err):
        """PUT obj as JSON (update, e.g. a geofence)."""
        self._request("PUT", path, json.dumps(obj).encode("utf-8"), on_ok, on_err)

    def delete(self, path, on_ok, on_err):
        """DELETE (e.g. a device); on_ok(None) — Traccar answers 204 with no body."""
        self._request("DELETE", path, None, on_ok, on_err)

    def _request(self, method, path, body, on_ok, on_err):
        login = self.creds()
        if not login:
            on_err("No saved Traccar login — set it up in Settings… → Connection")
            return
        url = self.cfg["server_url"].rstrip("/") + path
        req = QNetworkRequest(QUrl(url))
        token = base64.b64encode(("%s:%s" % login).encode("utf-8")).decode()
        req.setRawHeader(b"Authorization", ("Basic " + token).encode())
        req.setRawHeader(b"Accept", b"application/json")
        try:
            req.setTransferTimeout(30000)
        except AttributeError:     # pragma: no cover
            pass
        nam = QgsNetworkAccessManager.instance()
        if method in ("POST", "PUT"):
            req.setRawHeader(b"Content-Type", b"application/json")
            reply = nam.post(req, body) if method == "POST" else nam.put(req, body)
        elif method == "DELETE":
            reply = nam.deleteResource(req)
        else:
            reply = nam.get(req)
        self._pending.add(reply)

        def finished():
            self._pending.discard(reply)
            status = reply.attribute(_HTTP_STATUS)
            err = reply.error()
            errstr = reply.errorString()
            data = bytes(reply.readAll())
            reply.deleteLater()
            if err == _NET_OK and (status is None or int(status) in (200, 204)):
                if not data.strip() and method == "DELETE":
                    on_ok(None)
                    return
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception as exc:
                    on_err("Unexpected response from server: %s" % exc)
                    return
                on_ok(obj)
            elif status:
                status = int(status)
                reason = data.decode("utf-8", "replace").strip().split(" - ")[0][:150] if method != "GET" else ""
                on_err("Wrong username or password (HTTP 401)" if status == 401
                       else "HTTP %d on %s%s" % (status, path.split("?")[0], (": " + reason) if reason else ""))
            else:
                on_err("No response — check the server URL (%s)" % errstr)

        reply.finished.connect(finished)

    def abort_all(self):
        for r in list(self._pending):
            try:
                r.abort()
            except RuntimeError:
                pass
        self._pending.clear()


# ════════════════════════════════════════════════════════════════════════════
#  Send point — a clicked map point becomes a fix of one Traccar device, sent with
#  the OsmAnd protocol (plain HTTP, port 5055 by default, no login: the device
#  identifier is the key, as for the phone apps).
# ════════════════════════════════════════════════════════════════════════════

OSMAND_PORT = 5055
SENT_POINT_PREFIX = "qgis-"     # identifier of devices added by Send point (the only ones it may delete)


def osmand_url(cfg):
    """The OsmAnd endpoint: the 'Send to' setting, else http://<server host>:5055/."""
    if cfg["send_url"]:
        url = cfg["send_url"]
        return url if "://" in url else "http://" + url
    host = QUrl(cfg["server_url"] or DEFAULT_URL).host()
    return "http://%s:%d/" % (host, OSMAND_PORT) if host else ""


class _ClickTool(QgsMapToolEmitPoint):
    """Map tool: a left click → on_click(lat, lon) in WGS84; Esc → on_cancel(); on_off() when another
    map tool takes over."""

    def __init__(self, plugin, canvas, on_click, on_off, on_cancel):
        super().__init__(canvas)
        self.p = plugin
        self._on_click, self._on_off, self._on_cancel = on_click, on_off, on_cancel
        self.setCursor(QCursor(_e(Qt, "CursorShape", "CrossCursor")))
        self.canvasClicked.connect(self._clicked)

    def _clicked(self, point, button):
        if button != _e(Qt, "MouseButton", "LeftButton"):
            return
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), WGS84, QgsProject.instance())
        try:
            ll = tr.transform(point)
        except QgsCsException:
            self.p._bar("That point can't be converted to latitude / longitude", _MSG_WARN)
            return
        self._on_click(ll.y(), ll.x())

    def keyPressEvent(self, e):
        if e.key() == _e(Qt, "Key", "Key_Escape"):
            e.accept()
            self._on_cancel()

    def deactivate(self):
        super().deactivate()
        self._on_off()


class _ShapeTool(QgsMapTool):
    """Map tool for an area or a line: left-click each corner, then right-click (or Enter) to finish.
    Backspace removes the last corner, Esc clears the corners (or, with none, stops adding).
    Drawn as it grows: the shape, a dot on each corner and a dashed line to the mouse.
    on_done(kind, [(lat, lon), …]) in WGS84; the corners stay until reset(), so a cancelled send
    can carry on."""

    def __init__(self, plugin, canvas, kind, on_done, on_off, on_cancel):
        super().__init__(canvas)
        self.p, self.kind = plugin, kind
        self._on_done, self._on_off, self._on_cancel = on_done, on_off, on_cancel
        self.pts = []
        self.band = QgsRubberBand(canvas, _GEOM_POLY if kind == "area" else _GEOM_LINE)   # the shape
        self.rubber = QgsRubberBand(canvas, _GEOM_LINE)                                     # to the mouse
        self.dots = QgsRubberBand(canvas, _GEOM_POINT)                                      # the corners
        self.setCursor(QCursor(_e(Qt, "CursorShape", "CrossCursor")))
        self._style()

    def _style(self):
        color = QColor(self.p.cfg["geofence_color"])
        fill = QColor(color)
        fill.setAlpha(60)
        self.band.setStrokeColor(color)
        self.band.setFillColor(fill)
        self.band.setWidth(2)
        self.rubber.setStrokeColor(color)
        self.rubber.setWidth(1)
        self.rubber.setLineStyle(_e(Qt, "PenStyle", "DashLine"))
        self.dots.setColor(color)
        self.dots.setIcon(_e(QgsRubberBand, "IconType", "ICON_CIRCLE"))
        self.dots.setIconSize(9)
        self.dots.setWidth(2)

    def _redraw(self, moving=None):
        self._style()
        pts = [QgsPointXY(p) for p in self.pts]
        if self.kind == "area" and len(pts) >= 3:
            self.band.setToGeometry(QgsGeometry.fromPolygonXY([pts + pts[:1]]), None)
        elif len(pts) >= 2:
            self.band.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)
        else:
            self.band.reset(_GEOM_POLY if self.kind == "area" else _GEOM_LINE)
        if moving is not None and pts:                      # dashed line(s) from the last (and first) corner
            ends = [pts[-1], QgsPointXY(moving)] + ([pts[0]] if self.kind == "area" and len(pts) >= 2 else [])
            self.rubber.setToGeometry(QgsGeometry.fromPolylineXY(ends), None)
        else:
            self.rubber.reset(_GEOM_LINE)
        if pts:
            self.dots.setToGeometry(QgsGeometry.fromMultiPointXY(pts), None)
        else:
            self.dots.reset(_GEOM_POINT)

    def canvasMoveEvent(self, e):
        if self.pts:
            self._redraw(e.mapPoint())

    def canvasReleaseEvent(self, e):
        if e.button() == _e(Qt, "MouseButton", "LeftButton"):
            self.pts.append(QgsPointXY(e.mapPoint()))
            self._redraw(e.mapPoint())
            n = len(self.pts)
            self.p.tool_hint("%d corner%s — click the next; right-click or Enter to finish, Backspace to undo"
                             % (n, "" if n == 1 else "s"))
        elif e.button() == _e(Qt, "MouseButton", "RightButton"):
            self.finish()

    def keyPressEvent(self, e):
        key = e.key()
        if key == _e(Qt, "Key", "Key_Escape"):
            if self.pts:
                self.reset()
            else:
                self._on_cancel()
        elif key in (_e(Qt, "Key", "Key_Backspace"), _e(Qt, "Key", "Key_Delete")):
            self.pts = self.pts[:-1]
            self._redraw()
        elif key in (_e(Qt, "Key", "Key_Return"), _e(Qt, "Key", "Key_Enter")):
            self.finish()
        else:
            return
        e.accept()

    def finish(self):
        need = 3 if self.kind == "area" else 2
        if len(self.pts) < need:
            self.p._bar("An %s needs at least %d corners" % (self.kind, need), _MSG_WARN, 3)
            return
        tr = QgsCoordinateTransform(self.canvas().mapSettings().destinationCrs(), WGS84, QgsProject.instance())
        try:
            lls = [tr.transform(pt) for pt in self.pts]
        except QgsCsException:
            self.p._bar("Those corners can't be converted to latitude / longitude", _MSG_WARN)
            return
        self._redraw()
        self._on_done(self.kind, [(ll.y(), ll.x()) for ll in lls])

    def reset(self):
        self.pts = []
        self._redraw()

    def remove_bands(self):
        for band in (self.band, self.rubber, self.dots):
            try:
                self.canvas().scene().removeItem(band)
            except (RuntimeError, AttributeError):
                pass

    def deactivate(self):
        self.reset()
        super().deactivate()
        self._on_off()


# ════════════════════════════════════════════════════════════════════════════
#  Send dialog — the same form as the QField plugin: name, then the Traccar fields
#  that suit the shape, and a row of colour swatches (geofences).
# ════════════════════════════════════════════════════════════════════════════

SWATCHES = ("#E65100", "#C62828", "#AD1457", "#6A1B9A", "#1565C0",
            "#00838F", "#2E7D32", "#F9A825", "#4E342E", "#424242")      # same as the QField plugin
DEVICE_CATEGORIES = ("default", "animal", "bicycle", "boat", "bus", "camper", "car", "crane", "helicopter",
                     "motorcycle", "offroad", "person", "pickup", "plane", "scooter", "ship", "tractor",
                     "train", "tram", "trolleybus", "truck", "van")    # Traccar's map icons
KNOTS_PER_KMH = 1 / 1.852


class _SwatchRow(QWidget):
    """Colour swatches (one ticked); color() → "#RRGGBB"."""

    def __init__(self, color, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        color = (color or SWATCHES[0]).upper()
        self._color = color
        self.buttons = {}
        for c in SWATCHES + (() if color in SWATCHES else (color,)):     # keep a colour set elsewhere
            b = QToolButton()
            b.setFixedSize(26, 26)
            b.setCheckable(True)
            b.setToolTip(c)
            b.clicked.connect(lambda _on=False, c=c: self.set_color(c))
            self.buttons[c] = b
            lay.addWidget(b)
        lay.addStretch()
        self.set_color(self._color)

    def set_color(self, c):
        self._color = c
        for k, b in self.buttons.items():
            b.setChecked(k == c)
            b.setStyleSheet("QToolButton{background:%s;border:%s;border-radius:5px;}"
                            % (k, "3px solid black" if k == c else "1px solid #888"))

    def color(self):
        return self._color


class SendDialog(QDialog):
    """Name and details for one shape sent to Traccar. mode: "point" (a device), or "circle",
    "area", "line" (geofences). coords: [(lat, lon), …] in WGS84. values() after exec().
    edit: a geofence from Traccar (its raw JSON) — the form then edits it instead."""

    TITLES = {"point": "Send point", "circle": "Send circle as a geofence",
              "area": "Send area as a geofence", "line": "Send line as a geofence"}

    def __init__(self, plugin, mode, coords, parent=None, edit=None):
        super().__init__(parent)
        self.p, self.mode, self.coords, self.edit = plugin, mode, coords, edit
        title = "Edit geofence" if edit is not None else self.TITLES[mode]
        self.setWindowTitle("Traccar Live – " + title)
        f = QFormLayout(self)
        head = QLabel(title)
        head.setStyleSheet("font-weight:bold;font-size:14px;")
        f.addRow(head)
        if mode in ("point", "circle"):
            lat, lon = coords[0]
            where = plugin.format_location({"latitude": lat, "longitude": lon}) or format_latlon(lat, lon)
            f.addRow(_hint(("Centre: " if mode == "circle" else "At: ") + where))
        else:
            f.addRow(_hint("%d corners" % len(coords)))
        self.name = QLineEdit()
        f.addRow("Name:", self.name)
        if mode == "point":
            self.name.setText(plugin._send_name or plugin.cfg["send_device_id"])
            self.name.setPlaceholderText("e.g. Team 2 or Clue 4")
            names = sorted({(d.get("name") or "") for d in plugin.devices.values()} - {""}, key=str.lower)
            comp = QCompleter(names, self)
            comp.setCaseSensitivity(_e(Qt, "CaseSensitivity", "CaseInsensitive"))
            self.name.setCompleter(comp)
            f.addRow(_hint("A new name adds a new point (device) in Traccar; the name of an existing device "
                           "moves it here. The details below are for a new point."))
            self.uid = QLineEdit()
            self.uid.setPlaceholderText("made from the name (qgis-…)")
            self.category = QComboBox()
            for c in DEVICE_CATEGORIES:
                self.category.addItem(c.capitalize(), c)
            self.group = QComboBox()
            self.group.addItem("— none —", 0)
            for g in plugin.groups:
                self.group.addItem(g.get("name") or str(g["id"]), g["id"])
            self.phone, self.model, self.contact = QLineEdit(), QLineEdit(), QLineEdit()
            f.addRow("Identifier:", self.uid)
            f.addRow("Category (icon):", self.category)
            f.addRow("Group:", self.group)
            f.addRow("Phone:", self.phone)
            f.addRow("Model:", self.model)
            f.addRow("Contact:", self.contact)
        else:
            self.name.setPlaceholderText("e.g. Sector A")
            if edit is None:
                f.addRow(_hint("A geofence sent from QGIS or QField before with the same name is replaced."))
            self.description = QLineEdit()
            f.addRow("Description:", self.description)
            self.radius = QSpinBox()
            self.radius.setRange(10, 100000)
            self.radius.setSingleStep(50)
            self.radius.setSuffix(" m")
            self.radius.setValue(int(plugin.cfg["circle_radius"]))
            if mode == "circle":
                row = QHBoxLayout()
                row.addWidget(self.radius, 1)
                for r in CIRCLE_RADII:
                    b = QToolButton()
                    b.setText("%d m" % r)
                    b.clicked.connect(lambda _c=False, r=r: self.radius.setValue(r))
                    row.addWidget(b)
                f.addRow("Radius:", row)
            self.corridor = QSpinBox()
            self.corridor.setRange(0, 10000)
            self.corridor.setSuffix(" m")
            self.corridor.setSpecialValueText("server default")
            self.corridor.setToolTip("How close to the line a device must be to count as on it "
                                     "(Traccar's polylineDistance)")
            if mode == "line":
                f.addRow("Corridor width:", self.corridor)
            self.speed = QSpinBox()
            self.speed.setRange(0, 1000)
            self.speed.setSuffix(" km/h")
            self.speed.setSpecialValueText("none")
            self.speed.setToolTip("Traccar raises an overspeed alert inside this geofence above this speed")
            f.addRow("Speed limit:", self.speed)
            self.calendar = QComboBox()
            self.calendar.addItem("— always active —", 0)
            for c in plugin.calendars:
                self.calendar.addItem(c.get("name") or str(c["id"]), c["id"])
            self.calendar.setToolTip("Only active at the times of this Traccar calendar")
            f.addRow("Calendar:", self.calendar)
            now = QColor(str(((edit or {}).get("attributes") or {}).get("color") or ""))
            self.swatches = _SwatchRow(now.name() if edit is not None and now.isValid()
                                       else GEOFENCE_COLOR if edit is not None else plugin.cfg["geofence_color"])
            f.addRow("Colour:", self.swatches)
        if edit is not None:
            self._fill(edit)
        bb = QDialogButtonBox(_BTN_CANCEL)
        send = bb.addButton("Save to Traccar" if edit is not None else "Send to Traccar",
                            _e(QDialogButtonBox, "ButtonRole", "AcceptRole"))
        send.setDefault(True)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.name.selectAll()
        self.name.setFocus()

    def _fill(self, raw):
        """Edit: the geofence's current values into the form."""
        attrs = raw.get("attributes") or {}
        self.name.setText(raw.get("name") or "")
        self.description.setText(raw.get("description") or "")
        if attrs.get("speedLimit"):
            self.speed.setValue(int(round(float(attrs["speedLimit"]) * 1.852)))
        if attrs.get("polylineDistance"):
            self.corridor.setValue(int(round(float(attrs["polylineDistance"]))))
        i = self.calendar.findData(raw.get("calendarId") or 0)
        self.calendar.setCurrentIndex(max(0, i))
        m = re.search(r",\s*(-?\d+(?:\.\d+)?)\s*\)\s*$", raw.get("area") or "")
        if self.mode == "circle" and m:
            self.radius.setValue(int(round(float(m.group(1)))))
        color = QColor(str(attrs.get("color") or ""))
        self.swatches.set_color(color.name().upper() if color.isValid() else GEOFENCE_COLOR)

    def _accept(self):
        if not self.name.text().strip():
            self.name.setFocus()
            self.name.setPlaceholderText("give it a name first")
            return
        self.accept()

    def values(self):
        v = {"name": self.name.text().strip()}
        if self.mode == "point":
            v.update(unique_id=self.uid.text().strip(), category=self.category.currentData(),
                     group_id=self.group.currentData(), phone=self.phone.text().strip(),
                     model=self.model.text().strip(), contact=self.contact.text().strip())
        else:
            v.update(description=self.description.text().strip(), radius=self.radius.value(),
                     corridor_m=self.corridor.value() if self.mode == "line" else 0,
                     speed_kmh=self.speed.value(), calendar_id=self.calendar.currentData(),
                     color=self.swatches.color())
        return v


def _hint(text):
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color:#666;")
    return lbl


def geofence_extra(v):
    """Send dialog values → the geofence fields besides name and area (description, calendar, attributes)."""
    attrs = {"color": v["color"]}
    if v.get("speed_kmh"):
        attrs["speedLimit"] = round(v["speed_kmh"] * KNOTS_PER_KMH, 2)
    if v.get("corridor_m"):
        attrs["polylineDistance"] = v["corridor_m"]
    return {"description": v.get("description", ""), "calendarId": v.get("calendar_id") or 0, "attributes": attrs}


# ════════════════════════════════════════════════════════════════════════════
#  Geofences — Traccar's areas: POLYGON, LINESTRING or CIRCLE in WKT-like text with
#  LATITUDE FIRST ("POLYGON ((52.1 -9.5, …))", "CIRCLE (52.1 -9.5, 500)").
# ════════════════════════════════════════════════════════════════════════════

GEOFENCE_MARK = "createdByTraccarLiveQgis"   # attribute on geofences the plugin sent (the only ones re-sending replaces)
GEOFENCE_MAX_WKT = 4000                     # Traccar keeps the area in a 4096-character column
GEOFENCE_COLOR = "#E65100"
CIRCLE_RADII = (250, 500, 1000)
_EARTH_M_PER_DEG = 111320.0
_NUM = r"(-?\d+(?:\.\d+)?)"


def _ll(lat, lon):
    return "%.6f %.6f" % (lat, lon)


def _area_wkt(g):
    """A single line or polygon (WGS84) → Traccar area text; polygons keep their outer ring only."""
    if g.type() == _GEOM_LINE:
        return "LINESTRING (%s)" % ", ".join(_ll(p.y(), p.x()) for p in g.asPolyline())
    rings = g.asPolygon()
    return "POLYGON ((%s))" % ", ".join(_ll(p.y(), p.x()) for p in rings[0]) if rings else ""


def geofence_areas(geom, crs, radius):
    """A feature's geometry → [(area text, simplified?)], one per part. Points become
    circles of radius metres; polygon holes are dropped (Traccar has none); shapes too
    detailed for Traccar's limit are simplified until they fit. May raise QgsCsException."""
    g = QgsGeometry(geom)
    if g.isEmpty():
        return []
    if crs.isValid() and crs != WGS84:
        g.transform(QgsCoordinateTransform(crs, WGS84, QgsProject.instance()))
    if QgsWkbTypes.isCurvedType(g.wkbType()):
        g.convertToStraightSegment()
    gtype, multi = g.type(), g.isMultipart()
    if gtype == _GEOM_POINT:
        pts = g.asMultiPoint() if multi else [g.asPoint()]
        return [("CIRCLE (%s, %.1f)" % (_ll(p.y(), p.x()), radius), False) for p in pts]
    if gtype == _GEOM_LINE:
        parts = [QgsGeometry.fromPolylineXY(ln) for ln in (g.asMultiPolyline() if multi else [g.asPolyline()])]
    elif gtype == _GEOM_POLY:
        parts = [QgsGeometry.fromPolygonXY([pg[0]]) for pg in (g.asMultiPolygon() if multi else [g.asPolygon()])
                 if pg]
    else:
        return []
    out = []
    for part in parts:
        wkt, tol, simplified = _area_wkt(part), 0.0, False
        while len(wkt) > GEOFENCE_MAX_WKT and tol < 1:
            tol = tol * 2 or 0.00001                          # degrees: ~1 m, doubling
            wkt, simplified = _area_wkt(part.simplify(tol)), True
        if wkt and len(wkt) <= GEOFENCE_MAX_WKT:
            out.append((wkt, simplified))
    return out


def parse_geofence_area(area):
    """Traccar area text → ("polygon" | "line" | "circle", [(lon, lat), …]), or None.
    Circles come back as a 72-sided ring for drawing."""
    s = (area or "").strip()
    m = re.match(r"CIRCLE\s*\(\s*%s\s+%s\s*,\s*%s\s*\)" % (_NUM, _NUM, _NUM), s, re.I)
    if m:
        lat, lon, r = (float(v) for v in m.groups())
        k = _EARTH_M_PER_DEG
        kx = k * max(math.cos(math.radians(lat)), 0.01)
        return "circle", [(lon + r * math.sin(a) / kx, lat + r * math.cos(a) / k)
                          for a in (2 * math.pi * i / 72 for i in range(73))]
    m = re.match(r"(POLYGON|LINESTRING)\s*\(+(.*?)\)+\s*$", s, re.I | re.S)
    if not m:
        return None
    pts = []
    for pair in m.group(2).split(","):
        xy = pair.split()
        try:
            pts.append((float(xy[1]), float(xy[0])))
        except (IndexError, ValueError):
            return None
    return ("line" if m.group(1).upper() == "LINESTRING" else "polygon"), pts


# ════════════════════════════════════════════════════════════════════════════
#  GeoPackage template (same tables as Traccar_QField's template)
# ════════════════════════════════════════════════════════════════════════════

def _memory_uri(geom, schema, crs="EPSG:4326"):
    return geom + "?crs=" + crs + "".join("&field=%s:%s" % f for f in schema)


def default_gpkg_path():
    """Suggested file for New GeoPackage: traccar.gpkg in the project's home folder
    (else the last folder used, else the user's home), never an existing file."""
    folder = (QgsProject.instance().homePath()
              or QSettings().value(SETTINGS_NS + "/last_gpkg_dir", "", type=str)
              or os.path.expanduser("~"))
    path, n = os.path.join(folder, "traccar.gpkg"), 2
    while os.path.exists(path):
        path = os.path.join(folder, "traccar_%d.gpkg" % n)
        n += 1
    return path


def create_template_gpkg(path, crs_authid="EPSG:4326"):
    """Create traccar_points + traccar_tracks in one GeoPackage. Returns (points, tracks, errors)."""
    out, errors = [], []
    for geom, schema, table, action in (
            ("Point", POINT_SCHEMA, "traccar_points", _GPKG_NEW),
            ("LineStringZM", TRACK_SCHEMA, "traccar_tracks", _GPKG_LAYER)):
        tmp = QgsVectorLayer(_memory_uri(geom, schema, crs_authid), table, "memory")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = table
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = action
        res = QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, path, QgsProject.instance().transformContext(), opts)
        if res[0] != _VFW_OK:
            errors.append("%s: %s" % (table, res[1]))
            out.append(None)
            continue
        lyr = QgsVectorLayer("%s|layername=%s" % (path, table), table, "ogr")
        out.append(lyr if lyr.isValid() else None)
        if not lyr.isValid():
            errors.append("%s: could not open the new table" % table)
    return out[0], out[1], errors


# ════════════════════════════════════════════════════════════════════════════
#  Live view drawn on the map (no layers)
# ════════════════════════════════════════════════════════════════════════════

class _LiveItem(QgsMapCanvasItem):
    """Tracks, markers, labels and accuracy circles painted straight onto the map
    canvas. Covers the visible extent; QGIS calls updatePosition() on every pan /
    zoom. Points are held in the canvas CRS. Screen only — not in print layouts."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self.tracks = []     # [(QColor, QPolygonF in map coordinates)]
        self.markers = []     # [{"pt", "acc_pt", "color", "label", "tip"}]
        self.show_markers = self.show_labels = self.show_tracks = True
        self.show_accuracy = False
        self.setZValue(50)
        self.updatePosition()

    def updatePosition(self):
        self.setRect(self._canvas.mapSettings().visibleExtent())

    def _map_to_item(self, off):
        """Map coordinates → item pixels as one affine transform (pan, zoom and rotation)."""
        ext = self._canvas.extent()
        c, d = ext.center(), (max(ext.width(), ext.height()) or 1.0)
        p0 = self.toCanvasCoordinates(c) - off
        px = self.toCanvasCoordinates(QgsPointXY(c.x() + d, c.y())) - off
        py = self.toCanvasCoordinates(QgsPointXY(c.x(), c.y() + d)) - off
        m11, m12 = (px.x() - p0.x()) / d, (px.y() - p0.y()) / d
        m21, m22 = (py.x() - p0.x()) / d, (py.y() - p0.y()) / d
        return QTransform(m11, m12, m21, m22,
                          p0.x() - m11 * c.x() - m21 * c.y(), p0.y() - m12 * c.x() - m22 * c.y())

    def paint(self, painter, _option=None, _widget=None):
        off = self.pos()

        def px(pt):
            return self.toCanvasCoordinates(pt) - off

        painter.setRenderHint(_ANTIALIAS, True)
        if self.show_tracks and self.tracks:
            # Every vertex, drawn natively: the map → screen step is one transform, not a Python loop
            painter.save()
            painter.setTransform(self._map_to_item(off), True)
            for color, poly in self.tracks:
                pen = QPen(color)
                pen.setWidthF(3.0)
                pen.setCosmetic(True)          # 3 px whatever the transform's scale
                pen.setCapStyle(_CAP_ROUND)
                pen.setJoinStyle(_JOIN_ROUND)
                painter.setPen(pen)
                painter.setBrush(QBrush(_NO_BRUSH))
                painter.drawPolyline(poly)
            painter.restore()
        if not self.show_markers:
            return
        font = QFont()
        font.setPointSizeF(9)
        font.setBold(True)
        for m in self.markers:
            c = px(m["pt"])
            if self.show_accuracy and m["acc_pt"] is not None:
                e = px(m["acc_pt"])
                r = math.hypot(e.x() - c.x(), e.y() - c.y())
                if 3 < r < 4000:
                    fill, edge = QColor(m["color"]), QColor(m["color"])
                    fill.setAlpha(40)
                    edge.setAlpha(150)
                    painter.setPen(QPen(edge, 1))
                    painter.setBrush(QBrush(fill))
                    painter.drawEllipse(c, r, r)
            painter.setPen(QPen(QColor("white"), 2))
            painter.setBrush(QBrush(m["color"]))
            painter.drawEllipse(c, 6.5, 6.5)
            if self.show_labels and m["label"]:
                path = QPainterPath()
                path.addText(c + QPointF(9, -7), font, m["label"])
                painter.strokePath(path, QPen(QColor(255, 255, 255, 230), 3))
                painter.fillPath(path, QBrush(m["color"].darker(130)))


class _GeofenceItem(_LiveItem):
    """Traccar geofences painted onto the map under the live view: outline, light fill
    (not for lines) and the name, each in its own colour. They never go grey — a geofence
    isn't a fix that gets old. shapes: [{"poly": QPolygonF in map coordinates,
    "closed": bool, "label": str, "label_pt": QgsPointXY, "color": "#rrggbb"}]."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.shapes = []
        self.setZValue(40)

    def paint(self, painter, _option=None, _widget=None):
        if not self.shapes:
            return
        off = self.pos()
        painter.setRenderHint(_ANTIALIAS, True)
        painter.save()
        painter.setTransform(self._map_to_item(off), True)
        for s in self.shapes:
            edge = QColor(s["color"])
            fill = QColor(s["color"])
            fill.setAlpha(55)
            pen = QPen(edge)
            pen.setWidthF(2.0)
            pen.setCosmetic(True)
            pen.setJoinStyle(_JOIN_ROUND)
            painter.setPen(pen)
            if s["closed"]:
                painter.setBrush(QBrush(fill))
                painter.drawPolygon(s["poly"])
            else:
                painter.setBrush(QBrush(_NO_BRUSH))
                painter.drawPolyline(s["poly"])
        painter.restore()
        if not self.show_labels:
            return
        font = QFont()
        font.setPointSizeF(9)
        font.setItalic(True)
        for s in self.shapes:
            if not s["label"]:
                continue
            c = self.toCanvasCoordinates(s["label_pt"]) - off
            path = QPainterPath()
            path.addText(c + QPointF(-3.5 * len(s["label"]), 4), font, s["label"])
            painter.strokePath(path, QPen(QColor(255, 255, 255, 230), 3))
            painter.fillPath(path, QBrush(QColor(s["color"]).darker(140)))


class _PendingItem(_LiveItem):
    """Points just sent to Traccar, drawn at once until a refresh shows the real device:
    a dashed ring in the device's colour and "name (sending…)". points: [{"pt": QgsPointXY in
    map coordinates, "label": str, "color": "#rrggbb"}]."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.points = []
        self.setZValue(60)

    def paint(self, painter, _option=None, _widget=None):
        off = self.pos()
        painter.setRenderHint(_ANTIALIAS, True)
        font = QFont()
        font.setPointSizeF(9)
        font.setBold(True)
        font.setItalic(True)
        for m in self.points:
            c = self.toCanvasCoordinates(m["pt"]) - off
            color = QColor(m["color"])
            fill = QColor(color)
            fill.setAlpha(90)
            pen = QPen(color, 2.5)
            pen.setStyle(_e(Qt, "PenStyle", "DashLine"))
            painter.setPen(pen)
            painter.setBrush(QBrush(fill))
            painter.drawEllipse(c, 7.5, 7.5)
            path = QPainterPath()
            path.addText(c + QPointF(10, -8), font, m["label"])
            painter.strokePath(path, QPen(QColor(255, 255, 255, 230), 3))
            painter.fillPath(path, QBrush(color.darker(130)))


# ════════════════════════════════════════════════════════════════════════════
#  Main panel (dock widget: docked, tabbed or floating)
# ════════════════════════════════════════════════════════════════════════════

class MainDock(QgsDockWidget):
    def __init__(self, plugin, parent):
        super().__init__("Traccar Live", parent)
        self.p = plugin
        self.setObjectName("TraccarLiveDock")
        body = QWidget()
        body.setMinimumWidth(320)
        root = QVBoxLayout(body)

        # ── Time window ───────────────────────────────────────────────────
        tw = QGroupBox("Time window")
        twl = QVBoxLayout(tw)
        self.window_combo = QComboBox()
        for label, minutes in WINDOW_CHOICES:
            self.window_combo.addItem(label, minutes)
        twl.addWidget(self.window_combo)

        self.custom_w = QWidget()
        cl = QHBoxLayout(self.custom_w)
        cl.setContentsMargins(0, 0, 0, 0)
        self.from_edit = QDateTimeEdit()
        self.to_edit = QDateTimeEdit()
        for ed in (self.from_edit, self.to_edit):
            ed.setCalendarPopup(True)
            ed.setDisplayFormat("yyyy-MM-dd HH:mm")
        cl.addWidget(QLabel("From"))
        cl.addWidget(self.from_edit, 1)
        cl.addWidget(QLabel("To"))
        cl.addWidget(self.to_edit, 1)
        twl.addWidget(self.custom_w)

        self.feature_w = QWidget()
        fl = QGridLayout(self.feature_w)
        fl.setContentsMargins(0, 0, 0, 0)
        self.feature_combo = QComboBox()
        self.feature_reload = QToolButton()
        self.feature_reload.setText("↻")
        self.feature_reload.setToolTip("Re-read the features")
        self.span_combo = QComboBox()
        self.span_combo.addItems(["Its start → its end", "Its start + duration", "Its end − duration"])
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 14400)
        self.duration_spin.setSingleStep(15)
        self.duration_spin.setSuffix(" min")
        fl.addWidget(self.feature_combo, 0, 0, 1, 2)
        fl.addWidget(self.feature_reload, 0, 2)
        fl.addWidget(self.span_combo, 1, 0)
        fl.addWidget(self.duration_spin, 1, 1, 1, 2)
        twl.addWidget(self.feature_w)

        self.show_btn = QPushButton("Show this window")
        twl.addWidget(self.show_btn)
        self.status_lbl = QLabel()
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#555;")
        twl.addWidget(self.status_lbl)
        root.addWidget(tw)

        # ── Live / refresh / clear ────────────────────────────────────────
        row = QHBoxLayout()
        self.live_btn = QPushButton("▶  Live")
        self.live_btn.setCheckable(True)
        self.live_btn.setToolTip("Keep the window up to date in the background "
                                 "(also on the toolbar)")
        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setToolTip("Load the window once")
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Stop Live and remove the overlay from the map")
        row.addWidget(self.live_btn, 2)
        row.addWidget(self.refresh_btn, 1)
        row.addWidget(self.clear_btn, 1)
        self.add_btn = QPushButton("＋ Add")        # same Add menu as the QField plugin
        self.add_btn.setToolTip("Add a point, area, line or circle to Traccar, or send selected features")
        self.add_menu = QMenu(self.add_btn)
        for act in (self.p.act_send, self.p.act_area, self.p.act_line, self.p.act_circle):
            self.add_menu.addAction(act)
        self.add_menu.addSeparator()
        self.add_menu.addAction(self.p.act_send_sel)
        self.add_btn.setMenu(self.add_menu)
        row.addWidget(self.add_btn, 1)
        root.addLayout(row)

        # ── What to show ──────────────────────────────────────────────────
        show = QHBoxLayout()
        show.addWidget(QLabel("Show:"))
        self.chk_markers = QCheckBox("Markers")
        self.chk_labels = QCheckBox("Labels")
        self.chk_tracks = QCheckBox("Tracks")
        self.chk_accuracy = QCheckBox("Accuracy")
        self.chk_geofences = QCheckBox("Geofences")
        self.chk_geofences.setToolTip("Traccar's geofences (areas, lines, circles)")
        for c in (self.chk_markers, self.chk_labels, self.chk_tracks, self.chk_accuracy, self.chk_geofences):
            show.addWidget(c)
        show.addStretch()
        root.addLayout(show)

        # ── Devices | Geofences tabs ──────────────────────────────────────
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        dev_page = QWidget()
        dl = QVBoxLayout(dev_page)
        dl.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        self.devices_lbl = QCheckBox("Devices")       # also select all / none
        self.devices_lbl.setTristate(True)
        self.devices_lbl.setStyleSheet("font-weight:bold;")
        self.devices_lbl.setToolTip("Show all devices — or, when all are shown, hide them all")
        self.updated_lbl = QLabel()
        self.updated_lbl.setStyleSheet("color:#777;")
        self.zoom_all_btn = QToolButton()
        self.zoom_all_btn.setText("Zoom to all")
        head.addWidget(self.devices_lbl)
        head.addStretch()
        head.addWidget(self.updated_lbl)
        head.addWidget(self.zoom_all_btn)
        dl.addLayout(head)

        loc = QHBoxLayout()
        loc.addWidget(QLabel("Location as:"))
        self.coord_combo = QComboBox()
        loc.addWidget(self.coord_combo, 1)
        dl.addLayout(loc)

        self.table = QTableWidget(0, len(DEVICE_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(DEVICE_COLUMNS))
        self.table.setEditTriggers(_NO_EDIT)
        self.table.setSelectionBehavior(_SEL_ROWS)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(_CUSTOM_MENU)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, _HV_STRETCH)
        for c in range(1, len(DEVICE_COLUMNS)):
            hh.setSectionResizeMode(c, _HV_CONTENTS)
        self.table.setToolTip("Tick a device to show it on the map, double-click to centre the map "
                              "on it, ⋯ or right-click for more (save to file, show only this…). "
                              "Right-click the column titles to hide columns.")
        self.table.setMinimumHeight(140)
        dl.addWidget(self.table, 1)
        self.tabs.addTab(dev_page, "Devices")

        self.gf_box = QWidget()
        gl = QVBoxLayout(self.gf_box)
        gl.setContentsMargins(4, 4, 4, 4)
        self.gf_lbl = QCheckBox("Geofences")          # also show all / none
        self.gf_lbl.setTristate(True)
        self.gf_lbl.setStyleSheet("font-weight:bold;")
        self.gf_lbl.setToolTip("Show all geofences — or, when all are shown, hide them all")
        gl.addWidget(self.gf_lbl)
        self.gf_table = QTableWidget(0, len(GEOFENCE_COLUMNS))
        self.gf_table.setHorizontalHeaderLabels(list(GEOFENCE_COLUMNS))
        self.gf_table.setEditTriggers(_NO_EDIT)
        self.gf_table.setSelectionBehavior(_SEL_ROWS)
        self.gf_table.verticalHeader().setVisible(False)
        self.gf_table.setContextMenuPolicy(_CUSTOM_MENU)
        gh = self.gf_table.horizontalHeader()
        gh.setSectionResizeMode(0, _HV_STRETCH)
        for c in range(1, len(GEOFENCE_COLUMNS)):
            gh.setSectionResizeMode(c, _HV_CONTENTS)
        self.gf_table.setToolTip("Tick a geofence to show it on the map, double-click to zoom to it, "
                                 "⋯ or right-click to change its colour or delete it. "
                                 "Right-click the column titles to hide columns.")
        self.gf_table.setMinimumHeight(140)
        gl.addWidget(self.gf_table, 1)
        self.gf_empty = _hint("No geofences in Traccar yet — add one with Area, Line or Circle below, "
                              "or send selected features.")
        gl.addWidget(self.gf_empty)
        self.tabs.addTab(self.gf_box, "Geofences")

        for table, key, cols in ((self.table, "device_columns_hidden", DEVICE_COLUMNS),
                                 (self.gf_table, "geofence_columns_hidden", GEOFENCE_COLUMNS)):
            hdr = table.horizontalHeader()
            hdr.setContextMenuPolicy(_CUSTOM_MENU)
            hdr.customContextMenuRequested.connect(
                lambda _pos, t=table, k=key, cs=cols: self._columns_menu(t, k, cs))
            self._apply_hidden_columns(table, key, cols)

        # ── Playback ──────────────────────────────────────────────────────
        pb = QGroupBox("Playback")
        pl = QVBoxLayout(pb)
        r1 = QHBoxLayout()
        self.pb_start = QToolButton()
        self.pb_start.setText("⏮")
        self.pb_start.setToolTip("Go to the first fix in the window")
        self.pb_play = QToolButton()
        self.pb_play.setText("▶")
        self.pb_play.setCheckable(True)
        self.pb_play.setToolTip("Play / pause the tracks in the time window")
        self.pb_stop = QToolButton()
        self.pb_stop.setText("■")
        self.pb_stop.setToolTip("Leave playback — back to the latest positions")
        self.pb_slider = QSlider(_e(Qt, "Orientation", "Horizontal"))
        self.pb_slider.setToolTip("Drag to move through the time window")
        for w in (self.pb_start, self.pb_play, self.pb_stop):
            r1.addWidget(w)
        r1.addWidget(self.pb_slider, 1)
        pl.addLayout(r1)
        r_at = QHBoxLayout()
        self.pb_time = QDateTimeEdit()
        self.pb_time.setCalendarPopup(True)
        self.pb_time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.pb_time.setToolTip("Go to a time (local) in the tracks")
        self.pb_pan = QToolButton()
        self.pb_pan.setText("⌖")
        self.pb_pan.setToolTip("Pan to the selected device where it is shown — "
                               "or to all devices shown when none is selected")
        r_at.addWidget(QLabel("At:"))
        r_at.addWidget(self.pb_time, 1)
        r_at.addWidget(self.pb_pan)
        pl.addLayout(r_at)
        r2 = QHBoxLayout()
        self.pb_lbl = QLabel()
        self.pb_lbl.setStyleSheet("color:#555;")
        self.pb_speed = QComboBox()
        for label, speed in PLAY_SPEEDS:
            self.pb_speed.addItem(label, speed)
        r2.addWidget(self.pb_lbl, 1)
        r2.addWidget(QLabel("Speed:"))
        r2.addWidget(self.pb_speed)
        pl.addLayout(r2)
        root.addWidget(pb)

        # ── Save ──────────────────────────────────────────────────────────
        save = QHBoxLayout()
        self.save_pos_btn = QPushButton("📍  Save positions")
        self.save_trk_btn = QPushButton("〰  Save tracks")
        for b in (self.save_pos_btn, self.save_trk_btn):
            b.setToolTip("Save the devices shown (ticked) to the layer chosen in Settings → Layers")
            save.addWidget(b)
        root.addLayout(save)
        self.saves_to_lbl = QLabel()
        self.saves_to_lbl.setWordWrap(True)
        self.saves_to_lbl.setTextFormat(_e(Qt, "TextFormat", "RichText"))
        root.addWidget(self.saves_to_lbl)

        # ── Footer ────────────────────────────────────────────────────────
        foot = QHBoxLayout()
        self.settings_btn = QPushButton("Settings…")
        self.help_btn = QPushButton("Help")
        self.gf_btn = QToolButton()
        self.gf_btn.setText("⬡  Geofences")
        self.gf_btn.setToolTip("Reload or delete geofences (add them with ＋ Add)")
        self.gf_btn.setPopupMode(_e(QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        self.gf_menu = QMenu(self.gf_btn)
        self.gf_btn.setMenu(self.gf_menu)
        foot.addWidget(self.settings_btn)
        foot.addWidget(self.help_btn)
        foot.addStretch()
        foot.addWidget(self.gf_btn)
        root.addLayout(foot)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_NO_FRAME)
        scroll.setWidget(body)
        self.setWidget(scroll)

        # ── Wiring ────────────────────────────────────────────────────────
        self.window_combo.activated.connect(self._window_chosen)
        self.show_btn.clicked.connect(self._show_clicked)
        self.feature_reload.clicked.connect(self.reload_features)
        self.feature_combo.activated.connect(self._feature_chosen)
        self.span_combo.activated.connect(lambda i: self.p.set_cfg("feature_span", i))
        self.duration_spin.valueChanged.connect(lambda v: self.p.set_cfg("feature_duration", v))
        self.live_btn.toggled.connect(self.p.set_live)
        self.refresh_btn.clicked.connect(self.p.load_window)
        self.clear_btn.clicked.connect(self.p.clear)
        self.chk_markers.toggled.connect(lambda on: self.p.set_show("show_markers", on))
        self.chk_labels.toggled.connect(lambda on: self.p.set_show("show_labels", on))
        self.chk_tracks.toggled.connect(lambda on: self.p.set_show("show_tracks", on))
        self.chk_accuracy.toggled.connect(lambda on: self.p.set_show("show_accuracy", on))
        self.chk_geofences.toggled.connect(lambda on: self.p.set_show("show_geofences", on))
        self.gf_menu.aboutToShow.connect(self._fill_geofence_menu)
        self.coord_combo.activated.connect(
            lambda _i: self.p.set_coord_format(self.coord_combo.currentData()))
        self.pb_start.clicked.connect(self._play_from_start)
        self.pb_play.toggled.connect(self.p.play)
        self.pb_stop.clicked.connect(self.p.stop_playback)
        self.pb_slider.valueChanged.connect(self._slider_moved)
        self.pb_time.dateTimeChanged.connect(self._time_typed)
        self.pb_pan.clicked.connect(lambda: self.p.pan_to_shown(self._selected_id()))
        self.pb_speed.activated.connect(lambda _i: self.p.set_cfg("play_speed", self.pb_speed.currentData()))
        self.table.cellDoubleClicked.connect(self._zoom_row)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.itemChanged.connect(self._item_changed)
        self.table.customContextMenuRequested.connect(
            lambda pos: self._device_menu(self.table.rowAt(pos.y())))
        self.gf_table.cellDoubleClicked.connect(
            lambda r, _c: 0 <= r < len(self._gf_rows) and self.p.zoom_to_geofence(self._gf_rows[r]["id"]))
        self.gf_table.cellClicked.connect(lambda r, c: c == _GF_MENU_COL and self._geofence_row_menu(r))
        self.gf_table.itemChanged.connect(self._gf_item_changed)
        self.gf_table.customContextMenuRequested.connect(
            lambda pos: self._geofence_row_menu(self.gf_table.rowAt(pos.y())))
        self.gf_lbl.clicked.connect(lambda: self.p.set_all_geofences_shown(not self._all_gf_shown()))
        self.zoom_all_btn.clicked.connect(self.p.zoom_to_all)
        self.devices_lbl.clicked.connect(
            lambda: self.p.hide_all() if all(r["shown"] for r in self._rows) else self.p.show_all())
        self.save_pos_btn.clicked.connect(lambda: self.p.save_positions())
        self.save_trk_btn.clicked.connect(lambda: self.p.save_tracks())
        self.saves_to_lbl.linkActivated.connect(lambda _l: self.p.open_settings(1))
        self.settings_btn.clicked.connect(lambda: self.p.open_settings(0))
        self.help_btn.clicked.connect(self.p.show_help)
        self.visibilityChanged.connect(lambda vis: vis and self.refresh())
        self.dockLocationChanged.connect(self._area_changed)
        self.topLevelChanged.connect(lambda floating: self.p.set_cfg("dock_floating", bool(floating)))

        self._rows = []
        self._gf_rows = []
        self._play_shown = False

    # ── State → widgets ───────────────────────────────────────────────────
    def load_state(self):
        c = self.p.cfg
        idx = self.window_combo.findData(c["window_minutes"])
        self.window_combo.setCurrentIndex(idx if idx >= 0 else 2)
        now = QDateTime.currentDateTime()
        f = QDateTime.fromString(c["custom_from"], "yyyy-MM-dd HH:mm")
        t = QDateTime.fromString(c["custom_to"], "yyyy-MM-dd HH:mm")
        self.from_edit.setDateTime(f if f.isValid() else QDateTime(now.date(), QTime(0, 0)))
        self.to_edit.setDateTime(t if t.isValid() else now)
        self.span_combo.setCurrentIndex(c["feature_span"])
        self.duration_spin.blockSignals(True)
        self.duration_spin.setValue(c["feature_duration"])
        self.duration_spin.blockSignals(False)
        for chk, key in ((self.chk_markers, "show_markers"), (self.chk_labels, "show_labels"),
                         (self.chk_tracks, "show_tracks"), (self.chk_accuracy, "show_accuracy"),
                         (self.chk_geofences, "show_geofences")):
            chk.blockSignals(True)
            chk.setChecked(bool(c[key]))
            chk.blockSignals(False)
        if c["window_minutes"] == -2:
            self.reload_features()
        self.coord_combo.clear()
        for key, _label in COORD_FORMATS:
            self.coord_combo.addItem(self.p.coord_label(key), key)
        self.coord_combo.setCurrentIndex(max(0, self.coord_combo.findData(c["coord_format"])))
        self.pb_speed.setCurrentIndex(max(0, self.pb_speed.findData(c["play_speed"])))
        self.refresh_geofences()
        self._sync_mode()
        self.refresh()

    def _sync_mode(self):
        m = self.window_combo.currentData()
        self.custom_w.setVisible(m == -1)
        self.feature_w.setVisible(m == -2)
        self.show_btn.setVisible(m is not None and m < 0)
        self.duration_spin.setVisible(self.span_combo.currentIndex() > 0)

    def refresh(self):
        """Update everything that reflects the plugin's live state."""
        self.live_btn.blockSignals(True)
        self.live_btn.setChecked(self.p.live)
        self.live_btn.setText("⏹  Stop live" if self.p.live else "▶  Live")
        self.live_btn.blockSignals(False)
        self.refresh_btn.setEnabled(not self.p.loading)
        self.clear_btn.setEnabled(self.p.win is not None or self.p.live)
        self.status_lbl.setText(self.p.window_summary())
        self.updated_lbl.setText(("updated " + self.p.last_update) if self.p.last_update else "")
        self.duration_spin.setVisible(self.span_combo.currentIndex() > 0)

        rows = self.p.device_rows
        self._rows = rows
        shown = sum(1 for row in rows if row["shown"])
        self.tabs.setTabText(0, "Devices (%d)" % len(rows) if rows else "Devices")
        self.devices_lbl.setText("Devices  (%d)" % len(rows) if shown == len(rows)
                                 else "Devices  (%d of %d shown)" % (shown, len(rows)))
        self.devices_lbl.blockSignals(True)
        self.devices_lbl.setCheckState(_CHECKED if rows and shown == len(rows)
                                       else _UNCHECKED if shown == 0 else _PARTIAL)
        self.devices_lbl.setEnabled(bool(rows))
        self.devices_lbl.blockSignals(False)
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [row["name"], row["location"], row["fixes"], row["span"], row["speed"], row["battery"], "⋯"]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if col == 0:
                    item.setFlags(item.flags() | _ITEM_CHECKABLE)
                    item.setCheckState(_CHECKED if row["shown"] else _UNCHECKED)
                    item.setForeground(QColor(row["color"] if row["fresh"] and row["shown"] else "#9E9E9E"))
                    item.setToolTip(row["tip"])
                elif col == _MENU_COL:
                    item.setToolTip("Save to file, show only this device…")
                self.table.setItem(r, col, item)
        self.table.blockSignals(False)
        self.saves_to_lbl.setText(self.p.saves_to_html())
        self.refresh_playback()

    def refresh_playback(self):
        """Slider, buttons and time label (called on every playback frame)."""
        p = self.p
        rng = p.play_range()
        total = int((rng[1] - rng[0]).total_seconds()) if rng else 0
        for w in (self.pb_start, self.pb_play, self.pb_slider, self.pb_time):
            w.setEnabled(rng is not None)
        self.pb_pan.setEnabled(bool(p.devices))
        self.pb_stop.setEnabled(p.play_t is not None)
        self.pb_slider.blockSignals(True)
        self.pb_slider.setRange(0, total)
        self.pb_slider.setValue(int((p.play_t - rng[0]).total_seconds()) if rng and p.play_t else total)
        self.pb_slider.blockSignals(False)
        if rng and not self.pb_time.hasFocus():          # don't fight the user typing a time
            self.pb_time.blockSignals(True)
            self.pb_time.setDateTimeRange(_qdt_local(rng[0]), _qdt_local(rng[1]))
            self.pb_time.setDateTime(_qdt_local(p.play_t or rng[1]))
            self.pb_time.blockSignals(False)
        if p.play_t is not None or self._play_shown:     # Location, Speed, Battery: as shown on the map
            self._play_shown = p.play_t is not None
            for r, row in enumerate(self._rows):
                pos = p.shown_pos(row["id"])
                for col, text in ((1, p.format_location(pos) if pos else ""),
                                  (4, _speed_text(pos)), (5, _battery_text(pos))):
                    item = self.table.item(r, col)
                    if item is not None:
                        item.setText(text)
        self.pb_play.blockSignals(True)
        self.pb_play.setChecked(p.playing)
        self.pb_play.setText("⏸" if p.playing else "▶")
        self.pb_play.blockSignals(False)
        if p.play_t is not None:
            self.pb_lbl.setText(p.play_t.astimezone().strftime("%d %b %H:%M:%S") +
                                ("  — playing" if p.playing else "  — paused"))
        else:
            self.pb_lbl.setText("Replay the window: ▶ or drag" if rng else "No tracks to replay")

    # ── Handlers ──────────────────────────────────────────────────────────
    def _window_chosen(self, _i):
        m = self.window_combo.currentData()
        self.p.set_cfg("window_minutes", m)
        if m == -2:
            self.reload_features()
        self._sync_mode()
        if m > 0:
            self.p.reload_window()
        else:
            self.p.status_msg = "Set the window above, then click Show this window."
            self.refresh()

    def _show_clicked(self):
        if self.window_combo.currentData() == -1:
            self.p.set_cfg("custom_from", self.from_edit.dateTime().toString("yyyy-MM-dd HH:mm"))
            self.p.set_cfg("custom_to", self.to_edit.dateTime().toString("yyyy-MM-dd HH:mm"))
        self.p.reload_window()

    def reload_features(self):
        self.feature_combo.clear()
        feats = self.p.event_features()
        if not feats:
            self.feature_combo.addItem("— set up the layer in Settings → Advanced —", -1)
            return
        for f in feats:
            self.feature_combo.addItem(f["label"], f["fid"])
        idx = self.feature_combo.findData(self.p.cfg["event_feature_fid"])
        self.feature_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.p.set_cfg("event_feature_fid", self.feature_combo.currentData())

    def _feature_chosen(self, _i):
        self.p.set_cfg("event_feature_fid", self.feature_combo.currentData())

    def _zoom_row(self, r, _c):
        if 0 <= r < len(self._rows):
            self.p.pan_to_shown(self._rows[r]["id"])

    def _selected_id(self):
        r = self.table.currentRow()
        sel = self.table.selectionModel()
        if sel is not None and sel.hasSelection() and 0 <= r < len(self._rows):
            return self._rows[r]["id"]
        return None

    def _time_typed(self, qdt):
        self.p.set_play_time(datetime.fromtimestamp(qdt.toMSecsSinceEpoch() / 1000, timezone.utc))

    def _play_from_start(self):
        rng = self.p.play_range()
        if rng:
            self.p.set_play_time(rng[0])

    def _slider_moved(self, value):
        rng = self.p.play_range()
        if rng:
            self.p.set_play_time(rng[0] + timedelta(seconds=value))

    def _cell_clicked(self, r, c):
        if c == _MENU_COL:
            self._device_menu(r)

    def _item_changed(self, item):
        r = item.row()
        if item.column() == 0 and 0 <= r < len(self._rows):
            dev_id, on = self._rows[r]["id"], item.checkState() == _CHECKED
            # Later: the table is rebuilt, and this item replaced, while Qt still emits its signal
            QTimer.singleShot(0, lambda: self.p.set_device_shown(dev_id, on))

    def _device_menu(self, r):
        if not 0 <= r < len(self._rows):
            return
        row = self._rows[r]
        dev_id = row["id"]
        menu = QMenu(self.table)
        pos = self.p.shown_pos(dev_id)
        menu.addSection(row["name"] + ("  (at %s)" % pos["_t"].astimezone().strftime("%H:%M:%S")
                                       if pos is not None and self.p.play_t is not None else ""))
        act = menu.addAction("Pan to device", lambda: self.p.pan_to_shown(dev_id))
        act.setEnabled(pos is not None)
        menu.addAction("Show only this device", lambda: self.p.show_only(dev_id))
        menu.addAction("Show all devices", self.p.show_all)
        menu.addSeparator()
        self._copy_menus(menu, row)
        menu.addSeparator()
        menu.addAction("💾  Save to file (GPX, GeoPackage, CSV)…", lambda: self.p.save_device_file(dev_id))
        menu.addAction("📍  Save positions of this device", lambda: self.p.save_positions(only=dev_id))
        menu.addAction("〰  Save track of this device", lambda: self.p.save_tracks(only=dev_id))
        if self.p.is_sent_point(dev_id):            # only points added with 📤 Send point
            menu.addSeparator()
            menu.addAction("🗑  Delete this point from Traccar…", lambda: self.p.delete_sent_point(dev_id))
        menu.exec(QCursor.pos())
        menu.deleteLater()

    def _fill_geofence_menu(self):
        m, p = self.gf_menu, self.p
        m.clear()
        m.addAction("↻  Reload geofences", p.load_geofences)
        sub = m.addMenu("🗑  Delete from Traccar")
        sub.setEnabled(bool(p.geofences))
        for title, mine in (("Sent from QGIS / QField", True), ("Made in Traccar", False)):
            gfs = [g for g in p.geofences if g["mine"] == mine and not g.get("pending")]
            if gfs:
                sub.addSection(title)
            for g in gfs:
                sub.addAction(g["name"] or "(no name)", lambda gid=g["id"], n=g["name"]: p.delete_geofence(gid, n))

    def _all_gf_shown(self):
        hidden = self.p.hidden_geofence_ids()
        return bool(self._gf_rows) and not any(g["id"] in hidden for g in self._gf_rows)

    # ── Columns: right-click a list's column titles to hide / show them (remembered) ──
    def _hidden_columns(self, key):
        return {c for c in str(self.p.cfg[key]).split(",") if c}

    def _apply_hidden_columns(self, table, key, cols):
        hidden = self._hidden_columns(key)
        for i, title in enumerate(cols):
            table.setColumnHidden(i, i > 0 and title in hidden)

    def _columns_menu(self, table, key, cols):
        menu = QMenu(table)
        menu.addSection("Columns")
        hidden = self._hidden_columns(key)
        for i, title in enumerate(cols):
            if i == 0 or not title:                  # the name and ⋯ always stay
                continue
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(title not in hidden)
            act.toggled.connect(lambda on, t=title: self._set_column(table, key, cols, t, on))
        menu.addSeparator()
        menu.addAction("Show all columns", lambda: self._set_column(table, key, cols, None, True))
        menu.exec(QCursor.pos())
        menu.deleteLater()

    def _set_column(self, table, key, cols, title, on):
        hidden = self._hidden_columns(key)
        if title is None:
            hidden = set()
        elif on:
            hidden.discard(title)
        else:
            hidden.add(title)
        self.p.set_cfg(key, ",".join(c for c in cols if c in hidden))
        self._apply_hidden_columns(table, key, cols)

    def refresh_geofences(self):
        """The Geofences tab: list, header tick box and the tab's title."""
        p = self.p
        rows = list(p.geofences)
        self._gf_rows = rows
        hidden = p.hidden_geofence_ids()
        shown = sum(1 for g in rows if g["id"] not in hidden)
        self.tabs.setTabText(1, "Geofences (%d)" % len(rows) if rows else "Geofences")
        self.gf_empty.setVisible(not rows)
        self.gf_lbl.blockSignals(True)
        self.gf_lbl.setText("Geofences  (%d)" % len(rows) if shown == len(rows)
                            else "Geofences  (%d of %d shown)" % (shown, len(rows)))
        self.gf_lbl.setCheckState(_CHECKED if rows and shown == len(rows)
                                  else _UNCHECKED if shown == 0 else _PARTIAL)
        self.gf_lbl.setEnabled(bool(p.cfg["show_geofences"]))
        self.gf_lbl.blockSignals(False)
        self.gf_table.setEnabled(bool(p.cfg["show_geofences"]))
        self.gf_table.blockSignals(True)
        self.gf_table.setRowCount(len(rows))
        for r, g in enumerate(rows):
            on = g["id"] not in hidden
            for col, v in enumerate([g["name"] or "(no name)", p.geofence_type(g),
                                     "sending…" if g.get("pending") else "QGIS / QField" if g["mine"] else "Traccar",
                                     g["description"],
                                     "%d km/h" % g["speed_kmh"] if g["speed_kmh"] else "", "⋯"]):
                item = QTableWidgetItem(v)
                if col == 0:
                    item.setFlags(item.flags() | _ITEM_CHECKABLE)
                    item.setCheckState(_CHECKED if on else _UNCHECKED)
                    swatch = QPixmap(12, 12)
                    swatch.fill(QColor(g["color"]))
                    item.setIcon(QIcon(swatch))
                elif col == _GF_MENU_COL:
                    item.setToolTip("Zoom to, colour, delete…")
                self.gf_table.setItem(r, col, item)
        self.gf_table.blockSignals(False)

    def _gf_item_changed(self, item):
        r = item.row()
        if item.column() == 0 and 0 <= r < len(self._gf_rows):
            gid, on = self._gf_rows[r]["id"], item.checkState() == _CHECKED
            # Later: the table is rebuilt, and this item replaced, while Qt still emits its signal
            QTimer.singleShot(0, lambda: self.p.set_geofence_shown(gid, on))

    def _geofence_row_menu(self, r):
        if not 0 <= r < len(self._gf_rows):
            return
        g, p = self._gf_rows[r], self.p
        gid = g["id"]
        if g.get("pending"):                            # not in Traccar yet: nothing to change
            p._bar("'%s' is still being sent to Traccar" % g["name"], _MSG_INFO, 3)
            return
        menu = QMenu(self.gf_table)
        menu.addSection("%s  (%s, from %s)" % (g["name"] or "(no name)", p.geofence_type(g),
                                               "QGIS / QField" if g["mine"] else "Traccar"))
        menu.addAction("Zoom to geofence", lambda: p.zoom_to_geofence(gid))
        menu.addAction("✏  Edit…", lambda: p.edit_geofence(gid))
        colours = menu.addMenu("🎨  Colour")              # the same swatches as the send form and QField
        for c in SWATCHES:
            swatch = QPixmap(16, 16)
            swatch.fill(QColor(c))
            act = colours.addAction(QIcon(swatch), c + ("  ✓" if c == g["color"] else ""))
            act.triggered.connect(lambda _on=False, c=c: p.set_geofence_color(gid, c))
        colours.addSeparator()
        colours.addAction("Other colour…", lambda: p.set_geofence_color(gid))
        menu.addAction("📋  Copy as WKT", lambda: p.copy_text(p.geofence_wkt(gid), "WKT, WGS84"))
        menu.addSeparator()
        menu.addAction("🗑  Delete from Traccar…", lambda: p.delete_geofence(gid, g["name"]))
        menu.exec(QCursor.pos())
        menu.deleteLater()

    def _copy_menus(self, menu, row):
        """'Copy location' (each format, what3words, WKT) and 'Copy track' submenus."""
        p, dev_id = self.p, row["id"]
        pos = p.shown_pos(dev_id)              # in playback: where it is at the playback time
        sub = menu.addMenu("📋  Copy location" + ("  (playback time)" if p.play_t is not None else ""))
        sub.setEnabled(pos is not None)
        if pos is not None:
            for key, _label in COORD_FORMATS:
                text = p.format_location(pos, key)
                act = sub.addAction("%s:  %s" % (p.coord_label(key), text or "outside this grid / CRS"),
                                    lambda t=text, k=key: p.copy_text(t, p.coord_label(k)))
                act.setEnabled(bool(text))
            sub.addAction("what3words" if p.w3w_key() else "what3words…  (add an API key in Settings)",
                          lambda: p.copy_w3w(pos))
            sub.addSeparator()
            for crs, name in ((WGS84, "WGS84"), (QgsProject.instance().crs(), p.coord_label("project"))):
                sub.addAction("WKT point (%s)" % name,
                              lambda c=crs, n=name: p.copy_text(p.location_wkt(pos, c), "WKT point, " + n))
        n = len(p.tracks.get(dev_id, []))
        sub = menu.addMenu("📋  Copy track  (%d fixes in the window)" % n)
        sub.setEnabled(n > 0)
        for crs, name in ((WGS84, "WGS84"), (QgsProject.instance().crs(), p.coord_label("project"))):
            sub.addAction("WKT LineString (%s)" % name,
                          lambda c=crs, nm=name: p.copy_text(p.track_wkt(dev_id, c), "WKT track, " + nm))
        sub.addAction("One location per line (%s)" % p.coord_label(),
                      lambda: p.copy_text(p.track_lines(dev_id), "track, " + p.coord_label()))

    def _area_changed(self, area):
        for name, value in DOCK_AREAS.items():
            if value == area:
                self.p.set_cfg("dock_area", name)

    def save_geometry(self):
        """Remember where the panel floats (a docked panel's place is kept by dock_area)."""
        if self.isFloating():
            QSettings().setValue(SETTINGS_NS + "/dock_geometry", self.saveGeometry())


# ════════════════════════════════════════════════════════════════════════════
#  Settings (tabs: Connection, Layers, Tag, Advanced)
# ════════════════════════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    def __init__(self, plugin, parent, tab=0):
        super().__init__(parent)
        self.p = plugin
        self._closed = False
        c = plugin.cfg
        self.setWindowTitle("Traccar Live – Settings")
        self.setMinimumWidth(480)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # ── Connection ────────────────────────────────────────────────────
        t = QWidget()
        f = QFormLayout(t)
        self.url_edit = QLineEdit(c["server_url"])
        self.url_edit.setPlaceholderText(DEFAULT_URL)
        self.user_edit = QLineEdit(c["username"])
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(_ECHO_PWD)
        self._has_login = bool(c["authcfg"]) or bool(plugin._old_pw)
        if self._has_login:
            self.pass_edit.setPlaceholderText("saved — leave blank to keep it")
        f.addRow("Server URL:", self.url_edit)
        f.addRow("Email / username:", self.user_edit)
        f.addRow("Password:", self.pass_edit)
        test = QPushButton("Test connection")
        self.test_lbl = QLabel()
        self.test_lbl.setWordWrap(True)
        f.addRow(test)
        f.addRow(self.test_lbl)
        hint = QLabel("Use the address you open in a browser for Traccar. Your login is kept in "
                      "QGIS's password manager (Settings → Options → Authentication), protected "
                      "by QGIS's master password — not in the plugin's settings.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        f.addRow(hint)
        test.clicked.connect(self._test)

        g = QGroupBox("📤  Send point")
        gf = QFormLayout(g)
        self.send_id_edit = QLineEdit(c["send_device_id"])
        self.send_id_edit.setPlaceholderText("e.g. qgis-marker")
        self.send_url_edit = QLineEdit(c["send_url"])
        self.send_url_edit.setPlaceholderText(osmand_url(dict(c, send_url="")))
        gf.addRow("Default name:", self.send_id_edit)
        gf.addRow("Send to (OsmAnd):", self.send_url_edit)
        hint = QLabel("Each point clicked with <i>📤 Send point</i> asks for a name (this one is offered "
                      "first). A new name adds a new device with that name in Traccar at the point; the "
                      "name or identifier of an existing device moves that device there. Positions go to "
                      "Traccar's OsmAnd port (5055 unless your server uses another); leave <i>Send to</i> "
                      "blank for the server above on port 5055.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        gf.addRow(hint)
        f.addRow(g)
        self.tabs.addTab(t, "Connection")

        # ── Layers ────────────────────────────────────────────────────────
        t = QWidget()
        v = QVBoxLayout(t)

        g = QGroupBox("📍  Positions")
        gf = QFormLayout(g)
        self.pt_combo = QgsMapLayerComboBox()
        self.pt_combo.setFilters(_LF_POINT)
        self.pt_combo.setAllowEmptyLayer(True)
        self.pt_combo.setShowCrs(True)
        self.pt_name = QgsFieldComboBox()
        self.pt_name.setFilters(_FF_STRING)
        self.pt_name.setAllowEmptyFieldName(True)
        self.pt_latest = QRadioButton("Save the latest fix per device")
        self.pt_every = QRadioButton("Save every fix in the time window")
        self._pt_group = QButtonGroup(self)
        self._pt_group.addButton(self.pt_latest)
        self._pt_group.addButton(self.pt_every)
        gf.addRow("Layer:", self.pt_combo)
        gf.addRow("Device name into:", self.pt_name)
        gf.addRow(self.pt_latest)
        gf.addRow(self.pt_every)
        v.addWidget(g)

        g = QGroupBox("〰  Tracks")
        gf = QFormLayout(g)
        self.ln_combo = QgsMapLayerComboBox()
        self.ln_combo.setFilters(_LF_LINE)
        self.ln_combo.setAllowEmptyLayer(True)
        self.ln_combo.setShowCrs(True)
        self.ln_name = QgsFieldComboBox()
        self.ln_name.setFilters(_FF_STRING)
        self.ln_name.setAllowEmptyFieldName(True)
        self.ln_add = QRadioButton("Add a new track for each device on every save")
        self.ln_keep = QRadioButton("Keep only the most recent track per device")
        self._ln_group = QButtonGroup(self)
        self._ln_group.addButton(self.ln_add)
        self._ln_group.addButton(self.ln_keep)
        gf.addRow("Layer:", self.ln_combo)
        gf.addRow("Device name into:", self.ln_name)
        gf.addRow(self.ln_add)
        gf.addRow(self.ln_keep)
        v.addWidget(g)

        gp = QPushButton("New GeoPackage…  (creates both layers)")
        gp.clicked.connect(self._new_gpkg)
        v.addWidget(gp)
        hint = QLabel("The name always goes into a field called 'name' if the layer has one; "
                      "pick another text field (e.g. 'title') to fill that too. "
                      "Other fields are filled when the layer has them: device_id, fix_time, "
                      "fix_local, speed_kmh, course, altitude_m, accuracy_m, battery, address, "
                      "motion, fetched_at (positions); start_time, last_update, start_local, "
                      "last_local, from_time, to_time, n_points, saved_at (tracks).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        v.addWidget(hint)
        v.addStretch()
        self.tabs.addTab(t, "Layers")

        self._set_layer(self.pt_combo, c["points_layer_id"])
        self._set_layer(self.ln_combo, c["tracks_layer_id"])
        self.pt_name.setLayer(self.pt_combo.currentLayer())
        self.ln_name.setLayer(self.ln_combo.currentLayer())
        self.pt_name.setField(c["points_name_field"])
        self.ln_name.setField(c["tracks_name_field"])
        (self.pt_every if c["points_mode"] == 1 else self.pt_latest).setChecked(True)
        (self.ln_keep if c["tracks_mode"] == 1 else self.ln_add).setChecked(True)
        self.pt_combo.layerChanged.connect(self.pt_name.setLayer)
        self.ln_combo.layerChanged.connect(self.ln_name.setLayer)

        # ── Tag ───────────────────────────────────────────────────────────
        t = QWidget()
        f = QFormLayout(t)
        self.tag_chk = QCheckBox("Tag saved points and tracks")
        self.tag_chk.setChecked(bool(c["tag_enabled"]))
        self.tag_text = QLineEdit(c["tag_text"])
        self.tag_text.setPlaceholderText("e.g. FIRE-2026-001")
        self.tag_field = QComboBox()
        self.tag_field.setEditable(True)
        self.tag_feat = QCheckBox("With 'From feature', use the feature's display value instead")
        self.tag_feat.setChecked(bool(c["tag_from_feature"]))
        f.addRow(self.tag_chk)
        f.addRow("Tag text:", self.tag_text)
        f.addRow("Write into field:", self.tag_field)
        f.addRow(self.tag_feat)
        self._fill_tag_fields()
        self.pt_combo.layerChanged.connect(lambda _l: self._fill_tag_fields())
        self.ln_combo.layerChanged.connect(lambda _l: self._fill_tag_fields())
        self.tabs.addTab(t, "Tag")

        # ── Advanced ──────────────────────────────────────────────────────
        t = QWidget()
        v = QVBoxLayout(t)
        g = QGroupBox("Live")
        gf = QFormLayout(g)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(5, 3600)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(int(c["live_interval_s"]))
        self.stale_spin = QSpinBox()
        self.stale_spin.setRange(1, 1440)
        self.stale_spin.setSuffix(" min")
        self.stale_spin.setValue(int(c["stale_minutes"]))
        gf.addRow("Refresh every:", self.interval_spin)
        gf.addRow("Grey when last fix is older than:", self.stale_spin)
        v.addWidget(g)

        g = QGroupBox("Live view")
        gv = QVBoxLayout(g)
        self.view_canvas = QRadioButton("Drawn on the map — nothing in the Layers panel; "
                                        "hover a marker for details")
        self.view_layers = QRadioButton("As temporary layers (Temp Markers / Temp Tracks) — "
                                        "Identify, restyle, and include in print layouts")
        self._view_group = QButtonGroup(self)
        self._view_group.addButton(self.view_canvas)
        self._view_group.addButton(self.view_layers)
        (self.view_layers if c["live_view"] == "layers" else self.view_canvas).setChecked(True)
        gv.addWidget(self.view_canvas)
        gv.addWidget(self.view_layers)
        v.addWidget(g)

        g = QGroupBox("Location")
        gf = QFormLayout(g)
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setCrs(QgsCoordinateReferenceSystem(c["custom_crs"]))
        self.w3w_edit = QLineEdit()
        self.w3w_edit.setEchoMode(_ECHO_PWD)
        if plugin.w3w_key():
            self.w3w_edit.setPlaceholderText("saved — leave blank to keep it")
        self.w3w_forget = QCheckBox("Forget the saved key")
        self.w3w_forget.setVisible(bool(plugin.w3w_key()))
        self.latlon_combo = QComboBox()
        for key, label in LATLON_STYLES:
            self.latlon_combo.addItem(label, key)
        self.latlon_combo.setCurrentIndex(max(0, self.latlon_combo.findData(c["latlon_style"])))
        self.hemi_combo = QComboBox()
        for key, label in LATLON_HEMI:
            self.hemi_combo.addItem(label, key)
        self.hemi_combo.setCurrentIndex(max(0, self.hemi_combo.findData(c["latlon_hemi"])))
        gf.addRow("Lat / Long as:", self.latlon_combo)
        gf.addRow("Hemisphere:", self.hemi_combo)
        gf.addRow("Custom CRS:", self.crs_widget)
        gf.addRow("what3words API key:", self.w3w_edit)
        gf.addRow(self.w3w_forget)
        hint = QLabel("what3words needs an API plan that includes <i>convert-to-3wa</i>. "
                      "The key is kept in QGIS's password manager.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        gf.addRow(hint)
        v.addWidget(g)

        g = QGroupBox("Time window 'From feature'")
        gf = QFormLayout(g)
        self.ev_combo = QgsMapLayerComboBox()
        self.ev_combo.setFilters(_LF_VECTOR)
        self.ev_combo.setAllowEmptyLayer(True)
        self.ev_disp = QgsFieldComboBox()
        self.ev_start = QgsFieldComboBox()
        self.ev_end = QgsFieldComboBox()
        for fc in (self.ev_disp, self.ev_start, self.ev_end):
            fc.setAllowEmptyFieldName(True)
        gf.addRow("Layer (e.g. incidents):", self.ev_combo)
        gf.addRow("Name shown in the list:", self.ev_disp)
        gf.addRow("Start time field:", self.ev_start)
        gf.addRow("End time field (optional):", self.ev_end)
        v.addWidget(g)
        v.addStretch()
        self.tabs.addTab(t, "Advanced")

        self._set_layer(self.ev_combo, c["event_layer_id"])
        for fc in (self.ev_disp, self.ev_start, self.ev_end):
            fc.setLayer(self.ev_combo.currentLayer())
            self.ev_combo.layerChanged.connect(fc.setLayer)
        self.ev_disp.setField(c["event_display_field"])
        self.ev_start.setField(c["event_start_field"])
        self.ev_end.setField(c["event_end_field"])

        buttons = QDialogButtonBox(_BTN_OK | _BTN_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.tabs.setCurrentIndex(tab)

    @staticmethod
    def _set_layer(combo, layer_id):
        lyr = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        combo.setLayer(lyr)

    def _fill_tag_fields(self):
        if self._closed:          # closed dialog: ignore layers changing / project closing
            return
        current = self.tag_field.currentText() or self.p.cfg["tag_field"]
        names = []
        for combo in (self.pt_combo, self.ln_combo):
            lyr = combo.currentLayer()
            if lyr is not None:
                for n in lyr.fields().names():
                    if n not in names:
                        names.append(n)
        self.tag_field.clear()
        self.tag_field.addItems(names)
        self.tag_field.setCurrentText(current)

    def _test(self):
        self.test_lbl.setText("Testing…")
        cfg = dict(self.p.cfg)
        cfg.update(server_url=self.url_edit.text().strip().rstrip("/"))
        user, typed = self.user_edit.text().strip(), self.pass_edit.text()
        if typed:
            login = (user, typed)
        else:                                   # blank: test with the saved password
            saved = self.p._credentials()
            login = (user, saved[1]) if saved else None
        api = _Api(cfg, lambda: login)
        self._test_api = api      # keep alive until the reply arrives
        api.get("/api/devices",
                lambda d: self.test_lbl.setText(
                    "<span style='color:#2E7D32'>✓ Connected — %d device(s)</span>" % len(d)),
                lambda m: self.test_lbl.setText("<span style='color:#B71C1C'>✕ %s</span>" % m))

    def _new_gpkg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Create Traccar GeoPackage",
                                              default_gpkg_path(), "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        QSettings().setValue(SETTINGS_NS + "/last_gpkg_dir", os.path.dirname(path))
        pts, trk, errors = create_template_gpkg(path)
        for lyr, name, combo in ((pts, "Traccar Positions", self.pt_combo),
                                 (trk, "Traccar Tracks", self.ln_combo)):
            if lyr is not None:
                lyr.setName(name)
                QgsProject.instance().addMapLayer(lyr)
                combo.setLayer(lyr)
        if errors:
            QMessageBox.warning(self, "GeoPackage", "\n".join(errors))

    def values(self):
        pt = self.pt_combo.currentLayer()
        ln = self.ln_combo.currentLayer()
        ev = self.ev_combo.currentLayer()
        return {
            "server_url":  self.url_edit.text().strip().rstrip("/") or DEFAULT_URL,
            "username":    self.user_edit.text().strip(),
            "password":    self.pass_edit.text(),
            "send_device_id": self.send_id_edit.text().strip(),
            "send_url":       self.send_url_edit.text().strip(),
            "points_layer_id":   pt.id() if pt else "",
            "points_name_field": self.pt_name.currentField() if pt else "",
            "points_mode":       1 if self.pt_every.isChecked() else 0,
            "tracks_layer_id":   ln.id() if ln else "",
            "tracks_name_field": self.ln_name.currentField() if ln else "",
            "tracks_mode":       1 if self.ln_keep.isChecked() else 0,
            "tag_enabled":       self.tag_chk.isChecked(),
            "tag_text":          self.tag_text.text().strip(),
            "tag_field":         self.tag_field.currentText().strip(),
            "tag_from_feature":  self.tag_feat.isChecked(),
            "live_interval_s":   self.interval_spin.value(),
            "live_view":         "layers" if self.view_layers.isChecked() else "canvas",
            "custom_crs":        self.crs_widget.crs().authid() or self.p.cfg["custom_crs"],
            "latlon_style":      self.latlon_combo.currentData(),
            "latlon_hemi":       self.hemi_combo.currentData(),
            "w3w_key":           self.w3w_edit.text().strip(),
            "w3w_forget":        self.w3w_forget.isChecked(),
            "stale_minutes":     self.stale_spin.value(),
            "event_layer_id":      ev.id() if ev else "",
            "event_display_field": self.ev_disp.currentField() if ev else "",
            "event_start_field":   self.ev_start.currentField() if ev else "",
            "event_end_field":     self.ev_end.currentField() if ev else "",
        }


HELP_HTML = """
<h3>Getting started</h3>
<ol>
<li><b>Settings… → Connection</b>: enter your Traccar server and account, click <i>Test connection</i>.</li>
<li>Choose a <b>time window</b> and click <b>▶ Live</b> (or <b>↻ Refresh</b> to load it once).</li>
<li>To keep what you see, pick layers in <b>Settings… → Layers</b> (or click <i>New GeoPackage…</i>)
and use the <b>Save</b> buttons.</li>
</ol>
<h3>Time window</h3>
<p>One window controls everything: the tracks on the map are the fixes inside it, and the Save
buttons save exactly that.</p>
<ul>
<li><b>Last 15 minutes … Last 3 months</b> — follows the current time.</li>
<li><b>Custom dates</b> — From / To in local time, then <i>Show this window</i>.</li>
<li><b>From feature</b> — the start/end of a feature such as an incident (set the layer up once in
Settings → Advanced).</li>
</ul>
<p>Each device row shows how many fixes it has in the window and their time span.</p>
<h3>Devices</h3>
<ul>
<li><b>Tick / untick</b> a device to show or hide it on the map. Hidden devices are also left out of
<i>Zoom to all</i> and the Save buttons, and stay hidden next time.</li>
<li>The <b>Devices</b> tick box selects all / none: it shows every device, or hides them all when all are shown.</li>
<li><b>Double-click</b> a device to centre the map on it.</li>
<li><b>⋯</b> (or right-click) for: <i>Show only this device</i>, <i>Show all devices</i>,
<b>Save to file</b> — GPX (a track of the fixes in the window, with times and altitude, plus a waypoint
at the last fix), GeoPackage (the same two tables as below) or CSV (one row per fix) — and saving just
this device to your positions / tracks layers.</li>
<li><b>Copy location</b> (in the ⋯ menu) puts the device's position on the clipboard as Lat / Long,
Project CRS, a Custom CRS, Irish Grid (<i>O 15904 34671</i>), UK Grid (<i>NS 45140 72887</i>),
<b>what3words</b> or a WKT point. <b>Copy track</b> copies the fixes in the window as a WKT LineString,
or one location per line.</li>
<li><b>Location as</b> chooses the format of the <i>Location</i> column and the marker tooltips.
Lat / long as decimal degrees, degrees + minutes or degrees + minutes + seconds (with N/S/E/W or +/−),
the custom CRS and the what3words API key are set in Settings → Advanced → Location; what3words
needs a plan that includes <i>convert-to-3wa</i>, and is only asked when you copy.</li>
</ul>
<h3>Playback</h3>
<p>Replays the tracks in the time window. Click <b>▶</b> to play (pause with <b>⏸</b>), drag the slider to
move through time, or type / pick a time in <b>At:</b> to go straight to it; <b>⏮</b> goes back to the
first fix, and <b>Speed</b> runs from 1× (real time) to 1 hour per second. <b>⌖</b> pans to the selected
device where it is at that moment (or to all devices shown when none is selected); double-click and
<i>Pan to device</i> do the same, and <i>Copy location</i> in the ⋯ menu copies that position. The Location, Speed and Battery
columns show each device as it was at the playback time. Between fixes the accuracy circle uses the
larger of the two fixes' accuracy: the device was somewhere inside it. Each device's marker moves smoothly between its fixes and its track is drawn up to that
moment, with the rest of the track faint underneath (drawn-on-map view). Only the devices ticked in the
list are played. <b>■</b> leaves playback and shows the latest positions again; changing the time window
or <i>Clear</i> does too. Live updates keep coming in during playback.</p>
<h3>Live, Refresh and Clear</h3>
<p><b>▶ Live</b> refreshes in the background and keeps a “Last …” window moving. You can close this
panel — Live keeps running; the <b>▶ Live</b> toolbar button starts and stops it. A window that ends
in the past cannot change, so Live pauses for it and the markers show each device's last fix in it.
<b>↻ Refresh</b> loads the window once; <b>Clear</b> stops Live and removes the overlay.</p>
<h3>On the map</h3>
<p>By default the live view is <b>drawn straight onto the map</b>, so nothing is added to the Layers
panel: hover over a marker for its name, fix age, speed and battery. In <b>Settings → Advanced →
Live view</b> you can switch to <b>temporary layers</b> instead (<i>Temp Markers</i> and
<i>Temp Tracks</i> in the <i>Traccar (live)</i> group) — use those when you want Identify, your own
styling, or the live view in a print layout (drawn-on-map items appear on screen only). Either way
they are separate from the layers you save to (e.g. <i>Traccar Positions</i> / <i>Traccar Tracks</i>).
In both views each
device has its own colour (grey when its last fix is older than the limit in Settings → Advanced),
nothing is written to file, and QGIS won't ask to save anything. Switch Markers, Labels, Tracks and
Accuracy circles on or off in the panel.</p>
<h3>The panel</h3>
<p>The Traccar Live panel docks on the right of the map by default. Drag its title bar to dock it
elsewhere or tab it with other panels, or click its float button to use it as a separate window; it
opens where you left it. The <b>Traccar Live</b> toolbar button shows and hides it.</p>
<h3>Saving</h3>
<p>The Save buttons save the devices shown (ticked) in the list.<br>
<b>📍 Save positions</b> adds a point per device — its latest fix, or every fix in the window.<br>
<b>〰 Save tracks</b> adds one line per device for the window (Z = altitude, M = time), or replaces
that device's earlier track if <i>Keep only the most recent</i> is chosen.<br>
Fields are filled by name; fields your layer doesn't have are skipped. The device name can also
go into any text field (e.g. <i>title</i>). If the layer is already being edited, the features are
added to that edit session for you to save.</p>
<h3>Times</h3>
<p>Times here are local, including summer time. Date/time fields are stored in UTC (QGIS shows them
as UTC); the optional text fields <i>fix_local</i>, <i>start_local</i> and <i>last_local</i> hold the
local time. For a local date/time in labels use <code>datetime_from_epoch(epoch("fix_time"))</code>.</p>
<h3>Adding to Traccar</h3>
<p>The <b>＋ Add</b> button next to Clear (also the plugin menu) has the same tools as the
QField plugin:</p>
<ul>
<li><b>📤 Point</b> — click the map, give a name. A new name (e.g. <i>RV1</i>) adds a new device in Traccar
at that spot; the name of an existing device moves that device there (its earlier points stay in its
track). For a new point you can also set its identifier, category (its icon in Traccar), group, phone,
model and contact. To remove a point added this way, use <b>⋯ → Delete this point from Traccar…</b> on its
row (only offered for points whose identifier starts with <i>qgis-</i>, never for real trackers).
Positions go to Traccar's OsmAnd port 5055, or to the <i>Send to</i> address in
<b>Settings → Connection → Send point</b>.</li>
<li><b>⬡ Area</b> / <b>〰 Line</b> — click each corner, then right-click (or Enter) to finish; Backspace
removes the last corner, Esc clears the corners. The shape is drawn as you go, with a dashed line to the
mouse.</li>
<li><b>⭕ Circle</b> — click the centre; radius 250, 500, 1000 m or any other.</li>
<li><b>Right-click the map</b> → <i>Traccar Live</i> → <i>Send point here…</i> or <i>Send circle here…</i> at
that spot (and <i>Send selected features</i> when something is selected). In the Layers panel, right-click a
vector layer → <i>Send selected features to Traccar…</i>.</li>
<li><b>📑 Send selected features…</b> — the selected features of the active layer: areas and lines become
geofences, points become circles or points (devices). Name and description can come from fields. Any CRS
works; holes in polygons are dropped (Traccar has none), and very detailed shapes are simplified to fit
Traccar's size limit.</li>
</ul>
<p>While adding, a bar over the map says what to do and has a <b>Cancel</b> button (or press Esc). Once the
shape is sent, or cancelled, the map tool you had before (e.g. Pan) comes back — nothing to switch off.</p>
<p>Areas, lines and circles become Traccar <b>geofences</b>. The send form asks for a name, a description,
a <b>speed limit</b> (Traccar warns when a device goes faster inside it), the <b>corridor width</b> of a line
(how close a device must be to count as on it), a Traccar <b>calendar</b> (active only at those times) and a
<b>colour</b>. Sending a geofence with the same name as one sent from QGIS or QField before <b>replaces</b>
it, so you can edit a sector and send it again. Your Traccar account must be allowed to add devices and
geofences.</p>
<h3>Geofences</h3>
<p>Traccar's geofences are drawn on the map with their names, each in its own colour (the colour set in
Traccar, else orange) — they never go grey like old fixes. Tick <i>Geofences</i> under Show; they are
refreshed with Live. The <b>Geofences</b> tab lists them: tick one to show or hide it, double-click to zoom
to it, and ⋯ or right-click to <b>edit</b> it (name, description, speed limit, corridor width, calendar,
radius of a circle — the same form as for adding), <b>change its colour</b> (saved in Traccar, so its map
shows it too), copy it as WKT, or <b>delete it from Traccar</b> — you are always asked first, and deleting one made in Traccar gets
an extra warning, as Traccar alerts may use it. Right-click the column titles of either tab to hide columns
you don't need (remembered). Other Traccar users see a geofence when it is shared with them in Traccar
(admins see all).</p>
<h3>Tag</h3>
<p>Settings → Tag stamps a text such as FIRE-2026-001 onto everything you save. With
<i>From feature</i>, the feature's display value can be used instead.</p>
<p>Same layers and fields as the QField plugin <i>Traccar_QField</i>, so one GeoPackage works in both.</p>
<h3>About</h3>
<p><b>Not an official Traccar product.</b> Traccar Live is an independent, community-made plugin; it is not
made, endorsed or supported by Traccar or its developers, and is not affiliated with them.
<a href="https://www.traccar.org">Traccar</a> is open-source GPS tracking software created by Anton Tananaev
and its contributors — all credit for Traccar (the server, apps, API and documentation) goes to them. This
plugin only connects to a Traccar server you have access to, through the interfaces Traccar publishes. For
Traccar itself, see <a href="https://www.traccar.org">traccar.org</a>.</p>
"""


# ════════════════════════════════════════════════════════════════════════════
#  Plugin
# ════════════════════════════════════════════════════════════════════════════

class TraccarLive:

    def __init__(self, iface):
        self.iface = iface
        self.cfg = {}
        self._load_settings()
        self._login = None         # (username, password) for this session, from the auth manager
        self.api = _Api(self.cfg, self._credentials)
        self.timer = QTimer()
        self.timer.timeout.connect(self.poll_live)
        self.pending_timer = QTimer()     # re-checks after Traccar accepted a sent point
        self.pending_timer.setInterval(2000)
        self.pending_timer.timeout.connect(self._pending_tick)
        self.play_timer = QTimer()
        self.play_timer.timeout.connect(self._play_tick)
        self._actions = []
        self.dock = None

        # Runtime state
        self.live = False
        self.loading = False
        self.load_started = 0.0
        self.devices = {}      # dev_id → {name, status}
        self.win = None    # see _compute_window()
        self.tracks = {}      # dev_id → [positions in window], oldest first
        self.latest = {}      # dev_id → latest position
        self.marker_pos = {}      # dev_id → position drawn as the marker
        self.device_rows = []
        self.last_update = ""
        self.status_msg = ""
        self.last_error = ""
        self._mk_id = ""            # Temp layer ids ("layers" live view)
        self._tk_id = ""
        self._item = None          # canvas item ("canvas" live view)
        self._line_cache = {}      # dev_id → (fixes list, map CRS, [QPointF], QPolygonF) for drawing tracks
        self._tip_on = False
        self._w3w = None           # what3words API key, read from the password manager when first needed
        self.play_t = None         # playback time (aware UTC datetime), None = not in playback
        self.playing = False
        self._play_clock = 0.0
        self._pb_src, self._pb_times = None, {}    # fix times (epoch s) per device, for bisect
        self._w3w_reply = None
        self._tools = {}           # "send" / "circle" → _ClickTool
        self._send_replies = set()
        self.geofences = []        # [{id, name, area, mine, raw, color, kind}] from Traccar, by name
        self._gf_item = None       # canvas item drawing them
        self._send_name = ""       # last name sent to, offered again on the next click
        self.pending_points = []   # points just sent: drawn until a refresh shows the real device
        self._pending_item = None
        self.groups = []           # Traccar groups and calendars, for the send form
        self.calendars = []

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def initGui(self):
        mw = self.iface.mainWindow()
        self.act_open = QAction(QIcon(os.path.join(os.path.dirname(__file__), "icon.png")),
                                "Traccar Live", mw)
        self.act_open.setToolTip("Traccar Live — show / hide the panel (time window, devices, saving)")
        self.act_open.setCheckable(True)
        self.act_open.toggled.connect(self._toggle_panel)
        self.act_live = QAction("▶ Live", mw)
        self.act_live.setCheckable(True)
        self.act_live.setToolTip("Traccar Live: start / stop live updates")
        self.act_live.toggled.connect(self.set_live)
        self.act_send = QAction("📤 Point", mw)
        self.act_send.setToolTip("Traccar Live: click the map to send a point to Traccar "
                                 "(a new device, or move an existing one there)")
        self.act_send.triggered.connect(lambda _c=False: self.start_tool("send"))
        self.act_area = QAction("⬡ Area", mw)
        self.act_area.setToolTip("Traccar Live: click each corner of an area, right-click to finish — "
                                 "sent to Traccar as a geofence")
        self.act_area.triggered.connect(lambda _c=False: self.start_tool("area"))
        self.act_line = QAction("〰 Line", mw)
        self.act_line.setToolTip("Traccar Live: click each point of a line, right-click to finish — "
                                 "sent to Traccar as a geofence")
        self.act_line.triggered.connect(lambda _c=False: self.start_tool("line"))
        self.act_circle = QAction("⭕ Circle", mw)
        self.act_circle.setToolTip("Traccar Live: click the map to send a circle (250 / 500 / 1000 m …) "
                                   "to Traccar as a geofence")
        self.act_circle.triggered.connect(lambda _c=False: self.start_tool("circle"))
        self.act_send_sel = QAction("📑 Send selected features…", mw)
        self.act_send_sel.setToolTip("Send the selected polygons, lines or points of the active layer "
                                     "to Traccar: areas and lines as geofences, points as circles or points")
        self.act_send_sel.triggered.connect(self.send_selected)
        self.act_settings = QAction("Settings…", mw)
        self.act_settings.triggered.connect(lambda: self.open_settings(0))
        self.act_help = QAction("Help", mw)
        self.act_help.triggered.connect(self.show_help)
        for act in (self.act_open, self.act_live):        # the add tools are under ＋ Add and in the menu
            self.iface.addToolBarIcon(act)
        for act in (self.act_open, self.act_live, self.act_send, self.act_area, self.act_line, self.act_circle,
                    self.act_send_sel, self.act_settings, self.act_help):
            self.iface.addPluginToMenu(MENU_LABEL, act)
            self._actions.append(act)
        QgsProject.instance().readProject.connect(self._on_project_changed)
        QgsProject.instance().cleared.connect(self._on_project_changed)
        canvas = self.iface.mapCanvas()
        canvas.xyCoordinates.connect(self._on_canvas_move)
        canvas.destinationCrsChanged.connect(self._on_crs_changed)
        # Right-click: the map's menu (Pan, Identify… tools) and a vector layer's menu in the Layers panel
        canvas.contextMenuAboutToShow.connect(self._canvas_menu)
        self.act_layer_send = QAction("📑 Send selected features to Traccar…", mw)
        self.act_layer_send.triggered.connect(self.send_selected)
        self.iface.addCustomActionForLayerType(self.act_layer_send, "", _LAYER_VECTOR, True)
        self._remove_stale_overlays()

    def unload(self):
        self.timer.stop()
        self.play_timer.stop()
        self.api.abort_all()
        canvas = self.iface.mapCanvas()
        self._clear_tool_hint()
        for tool in self._tools.values():
            if canvas.mapTool() is tool:
                canvas.unsetMapTool(tool)
            if isinstance(tool, _ShapeTool):            # Area / Line tool's drawing
                tool.remove_bands()
        self._tools = {}
        self._remove_geofence_item()
        self.pending_timer.stop()
        self.pending_points = []
        self._draw_pending()
        for r in list(self._send_replies):
            try:
                r.abort()
            except RuntimeError:
                pass
        for sig, slot in ((QgsProject.instance().readProject, self._on_project_changed),
                          (QgsProject.instance().cleared, self._on_project_changed),
                          (canvas.xyCoordinates, self._on_canvas_move),
                          (canvas.destinationCrsChanged, self._on_crs_changed),
                          (canvas.contextMenuAboutToShow, self._canvas_menu)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        if getattr(self, "act_layer_send", None) is not None:
            self.iface.removeCustomActionForLayerType(self.act_layer_send)
            self.act_layer_send = None
        self._remove_overlays()
        if self.dock is not None:
            self.dock.save_geometry()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        for act in self._actions:
            self.iface.removePluginMenu(MENU_LABEL, act)
            self.iface.removeToolBarIcon(act)
        self._actions = []

    # ── Settings ──────────────────────────────────────────────────────────
    def _load_settings(self):
        s = QSettings()
        for key, default in DEFAULTS.items():
            v = s.value("%s/%s" % (SETTINGS_NS, key), default, type=type(default))
            self.cfg[key] = v
        # A plain-text password left by v0.1 / v0.2.0 — moved to the password manager on first use
        self._old_pw = s.value(SETTINGS_NS + "/password", None, type=str) or None
        if not self.cfg["v2_migrated"]:
            self._migrate_v1(s)

    def _migrate_v1(self, s):
        """v0.1 layers A / B / C → positions / tracks layers (once)."""
        def old(key, default, typ=str):
            return s.value("%s/%s" % (SETTINGS_NS, key), default, type=typ)
        live_id = old("live_layer_id", "")
        if old("append_pts", False, bool) and old("pt_layer_id", ""):
            self.cfg["points_layer_id"] = old("pt_layer_id", "")
        elif live_id not in ("", "<<temp>>"):
            self.cfg["points_layer_id"] = live_id
        if old("append_lines", False, bool) and old("ln_layer_id", ""):
            self.cfg["tracks_layer_id"] = old("ln_layer_id", "")
        interval = old("interval_min", 0, int)
        if interval:
            self.cfg["live_interval_s"] = max(5, min(3600, interval * 60))
        self.cfg["v2_migrated"] = True
        self._save_settings()

    def _save_settings(self):
        s = QSettings()
        for key in DEFAULTS:
            s.setValue("%s/%s" % (SETTINGS_NS, key), self.cfg[key])

    # ── Login (QGIS Authentication Manager) ───────────────────────────────
    def _credentials(self):
        """(username, password) for requests, or None. A plain-text password left
        by v0.1 / v0.2.0 is moved into the auth manager here, then deleted."""
        if self._login is not None:
            return self._login
        stored = load_login(self.cfg["authcfg"])
        if stored:
            self._login = stored
            return stored
        if self._old_pw:
            self.set_login(self.cfg["server_url"], self.cfg["username"], self._old_pw)
            return self._login
        return None

    def set_login(self, url, username, password):
        """Save the login in QGIS's password manager. A blank password keeps the
        saved one. Returns True when it is stored there."""
        if not password:
            old = load_login(self.cfg["authcfg"])
            if not old and self._old_pw:
                old = (self.cfg["username"], self._old_pw)
            password = old[1] if old else ""
        self.cfg["server_url"], self.cfg["username"] = url, username
        self._login = (username, password) if password else None
        if not password:
            return False
        authcfg = store_login(self.cfg["authcfg"], url, username, password)
        if authcfg:
            self.cfg["authcfg"] = authcfg
            self._old_pw = None
            QSettings().remove(SETTINGS_NS + "/password")
        else:
            self._bar("The login could not be saved in QGIS's password manager — "
                      "it will be asked for again next time QGIS starts.", _MSG_WARN)
        self._save_settings()
        return bool(authcfg)

    def set_cfg(self, key, value):
        if value is None:
            return
        self.cfg[key] = value
        QSettings().setValue("%s/%s" % (SETTINGS_NS, key), value)

    # ── Windows ───────────────────────────────────────────────────────────
    def open_main(self):
        """Show the panel where it was last: docked (right by default), tabbed or floating."""
        if self.dock is None:
            self.dock = MainDock(self, self.iface.mainWindow())
            self.iface.addDockWidget(DOCK_AREAS.get(self.cfg["dock_area"], DOCK_AREAS["right"]), self.dock)
            if self.cfg["dock_floating"]:
                self.dock.setFloating(True)
                geo = QSettings().value(SETTINGS_NS + "/dock_geometry")
                if geo:
                    self.dock.restoreGeometry(geo)
            self.dock.openedStateChanged.connect(self._panel_opened)
            self.dock.closed.connect(self.dock.save_geometry)
        self.dock.load_state()
        self.dock.setUserVisible(True)
        self.dock.raise_()

    def _toggle_panel(self, on):
        if on:
            self.open_main()
        elif self.dock is not None:
            self.dock.save_geometry()
            self.dock.setUserVisible(False)

    def _panel_opened(self, opened):
        self.act_open.blockSignals(True)
        self.act_open.setChecked(bool(opened))
        self.act_open.blockSignals(False)

    def open_settings(self, tab=0):
        dlg = SettingsDialog(self, self.iface.mainWindow(), tab)
        accepted = dlg.exec() == _DLG_OK
        vals = dlg.values() if accepted else None
        # A kept-alive dialog stays connected to the layer combos; closing / switching
        # projects then makes it touch layers being deleted (crashed QGIS 3.44).
        dlg._closed = True
        dlg.deleteLater()
        if accepted:
            interval_changed = vals["live_interval_s"] != self.cfg["live_interval_s"]
            view_changed = vals["live_view"] != self.cfg["live_view"]
            url, user, pwd = vals.pop("server_url"), vals.pop("username"), vals.pop("password")
            w3w, forget = vals.pop("w3w_key"), vals.pop("w3w_forget")
            if w3w or forget:
                self.set_w3w_key("" if forget else w3w)
            for k, v in vals.items():
                self.cfg[k] = v
            if pwd or url != self.cfg["server_url"] or user != self.cfg["username"]:
                self.set_login(url, user, pwd)
            self._save_settings()
            if view_changed:
                self._remove_overlays()
            if interval_changed and self.timer.isActive():
                self.timer.start(int(self.cfg["live_interval_s"]) * 1000)
            self._rebuild()
            if self.dock is not None and self.dock.isVisible():
                self.dock.load_state()

    def show_help(self):
        d = QDialog(self.iface.mainWindow())
        d.setWindowTitle("Traccar Live – Help")
        d.resize(560, 640)
        lay = QVBoxLayout(d)
        tb = QTextBrowser()
        tb.setHtml(HELP_HTML)
        lay.addWidget(tb)
        bb = QDialogButtonBox(_BTN_OK)
        bb.accepted.connect(d.accept)
        lay.addWidget(bb)
        d.exec()
        d.deleteLater()

    def _ui(self):
        if self.dock is not None and self.dock.isVisible():
            self.dock.refresh()
        tip = "Traccar Live: " + ("on" if self.live else "off")
        if self.last_update:
            tip += " — updated %s, %d device(s)" % (self.last_update, len(self.devices))
        if self.last_error:
            tip += "\n⚠ " + self.last_error
        if hasattr(self, "act_live"):
            self.act_live.setToolTip(tip)

    # ── Messages (unobtrusive) ────────────────────────────────────────────
    def _log(self, msg, level=_MSG_INFO):
        QgsMessageLog.logMessage(msg, "Traccar Live", level)

    def _bar(self, msg, level=_MSG_INFO, duration=5):
        self.iface.messageBar().pushMessage("Traccar Live", msg, level=level, duration=duration)
        self._log(msg, level)

    def _error(self, msg):
        """Report once per error streak; always logged."""
        if msg != self.last_error:
            self._bar(msg, _MSG_WARN, 8)
        else:
            self._log(msg, _MSG_WARN)
        self.last_error = msg
        self._ui()

    # ── Live ──────────────────────────────────────────────────────────────
    def set_live(self, on):
        on = bool(on)
        if on and not self.cfg["username"]:
            self._bar("Set up the connection first (Settings…)", _MSG_WARN)
            on = False
            self.open_settings(0)
        self.live = on
        self.act_live.blockSignals(True)
        self.act_live.setChecked(on)
        self.act_live.setText("⏹ Live" if on else "▶ Live")
        self.act_live.blockSignals(False)
        if on:
            self.timer.start(int(self.cfg["live_interval_s"]) * 1000)
            self.load_window()
        else:
            self.timer.stop()
        self._ui()

    def clear(self):
        self.stop_playback()
        if self.live:
            self.set_live(False)
        self.api.abort_all()
        self.loading = False
        self.win = None
        self.tracks, self.latest, self.marker_pos = {}, {}, {}
        self.status_msg = ""
        self._remove_overlays()
        self.geofences = []
        self._draw_geofences()
        self.pending_points = []
        self._draw_pending()
        self._rebuild()

    def reload_window(self):
        self.stop_playback()
        self.win = None
        self.tracks, self.latest, self.marker_pos = {}, {}, {}
        self.load_window()

    # ── Time window ───────────────────────────────────────────────────────
    def _compute_window(self):
        """{frm, to, end, moving, trim, tag} or {error}.
        moving — window includes now (Live adds fixes); trim — 'Last N' minutes;
        end — fixed future end of a moving custom/feature window (None = open)."""
        m = int(self.cfg["window_minutes"])
        now = datetime.now(timezone.utc)
        if m > 0:
            return {"frm": now - timedelta(minutes=m), "to": now, "end": None,
                    "moving": True, "trim": m, "tag": ""}
        tag = ""
        if m == -1:
            frm = _parse_local(self.cfg["custom_from"])
            to = _parse_local(self.cfg["custom_to"])
            if not frm or not to:
                return {"error": "Set From and To, then click Show this window."}
        else:
            f = self._selected_event_feature()
            if f is None:
                return {"error": "Choose a feature (set the layer up in Settings → Advanced)."}
            dur = timedelta(minutes=int(self.cfg["feature_duration"]))
            span = int(self.cfg["feature_span"])
            if span == 2:
                if f["end"] is None:
                    return {"error": "That feature has no end time."}
                to, frm = f["end"], f["end"] - dur
            else:
                if f["start"] is None:
                    return {"error": "That feature has no start time."}
                frm = f["start"]
                to = frm + dur if span == 1 else f["end"]      # None = ongoing
            if self.cfg["tag_from_feature"] and f["disp"]:
                tag = f["disp"]
        if to is not None and frm >= to:
            return {"error": "'From' must be before 'To'."}
        moving = to is None or to > now
        return {"frm": frm, "to": now if moving else to, "end": to if (moving and to) else None,
                "moving": moving, "trim": 0, "tag": tag}

    def window_summary(self):
        if self.status_msg:
            return self.status_msg
        w = self.win
        if w is None:
            return "Click ▶ Live to follow devices, or ↻ Refresh to load this window once."
        big = "\n⚠ Long window — loading and drawing may be slow." \
            if (w["to"] - w["frm"]) > timedelta(days=7) else ""
        span = _span_text(w["frm"], w["to"])
        if w["moving"]:
            return span + (" (still running)" if not w["trim"] else "") + "  ·  " + \
                ("updating live" if self.live else "Live is off") + big
        return span + "  ·  past window" + (" — Live paused" if self.live else "") + big

    # ── Event features ("From feature") ───────────────────────────────────
    def event_features(self):
        lyr = QgsProject.instance().mapLayer(self.cfg["event_layer_id"]) \
            if self.cfg["event_layer_id"] else None
        if lyr is None or not isinstance(lyr, QgsVectorLayer):
            return []
        names = lyr.fields().names()
        out = []
        for feat in lyr.getFeatures():
            def val(field):
                return feat[field] if field and field in names else None
            disp = val(self.cfg["event_display_field"])
            disp = "" if _is_null(disp) else str(disp)
            start = _attr_to_utc(val(self.cfg["event_start_field"]))
            end = _attr_to_utc(val(self.cfg["event_end_field"]))
            label = disp or "#%d" % feat.id()
            if start:
                label += "  (%s – %s)" % (_fmt_local(start), _fmt_local(end) if end else "ongoing")
            out.append({"fid": feat.id(), "disp": disp, "start": start, "end": end, "label": label})
        out.sort(key=lambda r: (r["start"] is not None, r["start"] or datetime.min.replace(tzinfo=timezone.utc),
                                r["fid"]), reverse=True)
        return out

    def _selected_event_feature(self):
        for f in self.event_features():
            if f["fid"] == int(self.cfg["event_feature_fid"]):
                return f
        return None

    # ── Loading (map only — never writes to layers) ───────────────────────
    def load_window(self):
        if self.loading and time.time() - self.load_started < 90:
            return
        if not self.cfg["username"]:
            self.status_msg = "Set up the connection first (Settings… → Connection)."
            self._ui()
            return
        w = self._compute_window()
        if "error" in w:
            self.status_msg = w["error"]
            self._ui()
            return
        self.loading, self.load_started = True, time.time()
        started = self.load_started
        self.status_msg = "Loading…"
        self._ui()
        self.load_geofences()

        def fail(msg):
            self.loading = False
            self.status_msg = ""
            self._error(msg)

        def got_devices(devs):
            self.devices = {d["id"]: {"name": d.get("name") or str(d["id"]),
                                      "status": d.get("status") or "",
                                      "uniqueId": d.get("uniqueId") or ""} for d in devs}

            def got_latest(positions):
                latest = {}
                for p in positions:
                    if self._prep(p):
                        latest[p["deviceId"]] = p
                ids = list(self.devices)
                by_dev = {}
                state = {"pending": len(ids), "failed": 0}

                def finish():
                    self.loading = False
                    self.status_msg = ""
                    self.win, self.tracks, self.latest = w, by_dev, latest
                    self.last_update = datetime.now().strftime("%H:%M:%S")
                    if state["failed"]:
                        self._error("%d device(s) could not be loaded" % state["failed"])
                    else:
                        self.last_error = ""
                    self._rebuild()
                    self._settle_pending(started)

                def one_done():
                    state["pending"] -= 1
                    if state["pending"] <= 0:
                        finish()

                if not ids:
                    finish()
                    return
                q_from, q_to = _quote(_iso(w["frm"])), _quote(_iso(w["to"]))
                for dev_id in ids:
                    def ok(hist, dev_id=dev_id):
                        valid = [p for p in hist if self._prep(p)]
                        valid.sort(key=lambda p: p["_t"])
                        if valid:
                            by_dev[dev_id] = valid
                        one_done()

                    def bad(_msg):
                        state["failed"] += 1
                        one_done()
                    self.api.get("/api/positions?deviceId=%s&from=%s&to=%s" % (dev_id, q_from, q_to),
                                 ok, bad)

            self.api.get("/api/positions", got_latest, fail)

        self.api.get("/api/devices", got_devices, fail)

    def poll_live(self):
        """Add each device's new fix to its track and move a 'Last …' window on."""
        if self.loading:
            return
        if self.win is None:
            self.load_window()
            return
        self.load_geofences()
        if not self.win["moving"]:
            return
        started = time.time()

        def fail(msg):
            self._error(msg)

        def got_devices(devs):
            self.devices = {d["id"]: {"name": d.get("name") or str(d["id"]),
                                      "status": d.get("status") or "",
                                      "uniqueId": d.get("uniqueId") or ""} for d in devs}

            def got_latest(positions):
                w = self.win
                if w is None:
                    return
                now = datetime.now(timezone.utc)
                end = w["end"]
                frm = now - timedelta(minutes=w["trim"]) if w["trim"] else w["frm"]
                tracks = dict(self.tracks)
                latest = {}
                for p in positions:
                    if not self._prep(p):
                        continue
                    latest[p["deviceId"]] = p
                    t = p["_t"]
                    if t < frm or (end is not None and t > end):
                        continue
                    arr = tracks.get(p["deviceId"], [])
                    if not arr or arr[-1]["_t"] < t:
                        tracks[p["deviceId"]] = arr + [p]
                if w["trim"]:
                    tracks = {k: [p for p in v if p["_t"] >= frm] for k, v in tracks.items()}
                    tracks = {k: v for k, v in tracks.items() if v}
                to = min(now, end) if end is not None else now
                self.win = dict(w, frm=frm, to=to, moving=(end is None or now < end))
                self.tracks, self.latest = tracks, latest
                self.last_update = now.astimezone().strftime("%H:%M:%S")
                self.last_error = ""
                self._rebuild()
                self._settle_pending(started)

            self.api.get("/api/positions", got_latest, fail)

        self.api.get("/api/devices", got_devices, fail)

    @staticmethod
    def _prep(p):
        """Validate a Traccar position and cache its parsed fix time as p['_t']."""
        if not isinstance(p, dict) or p.get("latitude") is None or p.get("longitude") is None:
            return False
        if "_t" not in p:
            p["_t"] = _parse_utc(p.get("fixTime"))
        return p["_t"] is not None

    def _fresh(self, t):
        return t is not None and \
            (datetime.now(timezone.utc) - t) < timedelta(minutes=int(self.cfg["stale_minutes"]))

    def _name(self, dev_id):
        return self.devices.get(dev_id, {}).get("name") or str(dev_id)

    # ── Rebuild overlay + device list from the loaded data ────────────────
    def _rebuild(self):
        w = self.win
        if w is not None and w["moving"]:
            mp = dict(self.latest)
        else:
            mp = {k: v[-1] for k, v in self.tracks.items() if v}
        self.marker_pos = mp

        rows, hidden = [], self.hidden_ids()
        for dev_id, info in self.devices.items():
            tr = self.tracks.get(dev_id, [])
            pos = mp.get(dev_id)
            rows.append({
                "id":      dev_id,
                "shown":   dev_id not in hidden,
                "name":    info["name"],
                "fixes":   len(tr) if tr else "—",
                "span":    (_span_text(tr[0]["_t"], tr[-1]["_t"]) if len(tr) > 1
                            else _fmt_local(tr[0]["_t"]) if tr else "no fixes in window"),
                "speed":   _speed_text(pos),
                "battery": _battery_text(pos),
                "pos":     pos,
                "fresh":   self._fresh(pos["_t"]) if pos else False,
                "color":   _device_color(info["name"]),
                "tip":     ("Last fix %s ago" % _age_text(pos["_t"])) if pos else "No position",
                "location": self.format_location(pos) if pos else "",
            })
        rows.sort(key=lambda r: r["name"].lower())
        self.device_rows = rows

        if w is not None:
            self._update_overlays()
        self._ui()

    # ════════════════════════════════════════════════════════════════════════
    #  Overlay layers (temporary, never saved)
    # ════════════════════════════════════════════════════════════════════════

    def _overlay(self, which):
        lid = self._mk_id if which == "markers" else self._tk_id
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is not None:
            return lyr
        if which == "markers":
            uri = _memory_uri("Point", [
                ("device_id", "integer"), ("name", "string(80)"), ("fix_time", "datetime"),
                ("fix_local", "string(40)"), ("speed_kmh", "double"), ("battery", "double"),
                ("accuracy_m", "double"), ("fresh", "integer"), ("color", "string(9)")])
            lyr = QgsVectorLayer(uri, "Temp Markers", "memory")
        else:
            uri = _memory_uri("LineStringZM", [
                ("device_id", "integer"), ("name", "string(80)"), ("fixes", "integer"),
                ("start_local", "string(40)"), ("last_local", "string(40)"),
                ("fresh", "integer"), ("color", "string(9)")])
            lyr = QgsVectorLayer(uri, "Temp Tracks", "memory")
        lyr.setCustomProperty(OVERLAY_PROP, 1)
        lyr.setCustomProperty("skipMemoryLayersCheck", 1)   # no "save scratch layers?" prompt
        root = QgsProject.instance().layerTreeRoot()
        group = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
        QgsProject.instance().addMapLayer(lyr, False)
        if which == "markers":
            group.insertLayer(0, lyr)
            self._mk_id = lyr.id()
            self._style_markers(lyr)
        else:
            group.addLayer(lyr)
            self._tk_id = lyr.id()
            self._style_tracks(lyr)
        self._apply_visibility()
        return lyr

    def _style_markers(self, lyr):
        color_expr = QgsProperty.fromExpression("if(\"fresh\" = 1, \"color\", '#9E9E9E')")
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "size": "3.4",
                                            "outline_color": "#ffffff", "outline_width": "0.6"})
        sym.symbolLayer(0).setDataDefinedProperty(_sl_prop("FillColor"), color_expr)
        if self.cfg["show_accuracy"]:
            acc = QgsSimpleMarkerSymbolLayer()
            acc.setColor(QColor(21, 101, 192, 35))
            acc.setStrokeColor(QColor(21, 101, 192, 150))
            acc.setStrokeWidth(0.2)
            acc.setSizeUnit(_RENDER_M)          # metres on the ground, any map CRS
            acc.setDataDefinedProperty(_sl_prop("Size"),
                                       QgsProperty.fromExpression('2 * coalesce("accuracy_m", 0)'))
            sym.insertSymbolLayer(0, acc)
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))

        pal = QgsPalLayerSettings()
        pal.fieldName = "name"
        fmt = QgsTextFormat()
        fmt.setSize(9)
        buf = QgsTextBufferSettings()
        buf.setEnabled(True)
        buf.setSize(0.8)
        buf.setColor(QColor("white"))
        fmt.setBuffer(buf)
        pal.setFormat(fmt)
        pal.dist = 1.2
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        lyr.setLabelsEnabled(bool(self.cfg["show_labels"]))
        lyr.triggerRepaint()

    @staticmethod
    def _style_tracks(lyr):
        sym = QgsLineSymbol.createSimple({"line_width": "0.7", "capstyle": "round",
                                          "joinstyle": "round"})
        sym.symbolLayer(0).setDataDefinedProperty(
            _sl_prop("StrokeColor"), QgsProperty.fromExpression("if(\"fresh\" = 1, \"color\", '#9E9E9E')"))
        sym.setOpacity(0.85)
        lyr.setRenderer(QgsSingleSymbolRenderer(sym))
        lyr.triggerRepaint()

    def _apply_visibility(self):
        root = QgsProject.instance().layerTreeRoot()
        for lid, key in ((self._mk_id, "show_markers"), (self._tk_id, "show_tracks")):
            node = root.findLayer(lid) if lid else None
            if node is not None:
                node.setItemVisibilityChecked(bool(self.cfg[key]))

    def set_show(self, key, on):
        self.set_cfg(key, bool(on))
        if key in ("show_geofences", "show_labels"):
            self._draw_geofences()
            if key == "show_geofences":
                return
        if self._item is not None:
            self._apply_item_flags()
            self._item.update()
            return
        mk = QgsProject.instance().mapLayer(self._mk_id) if self._mk_id else None
        if key == "show_labels" and mk is not None:
            mk.setLabelsEnabled(bool(on))
            mk.triggerRepaint()
        elif key == "show_accuracy" and mk is not None:
            self._style_markers(mk)
        else:
            self._apply_visibility()

    def set_live_view(self, mode):
        """'canvas' (drawn on the map) or 'layers' (Temp Markers / Temp Tracks)."""
        self.set_cfg("live_view", mode)
        self._remove_overlays()
        if self.win is not None:
            self._update_overlays()

    def _update_overlays(self):
        if self.cfg["live_view"] == "layers":
            self._update_layers()
        else:
            self._update_canvas()

    # ── Playback ──────────────────────────────────────────────────────────
    def play_range(self):
        """(first, last) fix time of the shown devices' tracks in the window, or None."""
        shown = [v for v in self._shown(self.tracks).values() if v]
        if not shown:
            return None
        return min(v[0]["_t"] for v in shown), max(v[-1]["_t"] for v in shown)

    def set_play_time(self, t):
        """Enter (or move) playback at time t, clamped to the tracks."""
        rng = self.play_range()
        if rng is None:
            return
        self.play_t = min(max(t, rng[0]), rng[1])
        self._play_frame()

    def play(self, on=True):
        rng = self.play_range()
        if on and rng is None:
            self._bar("Nothing to play back — no fixes in this window", _MSG_WARN)
            on = False
        if on:
            if self.play_t is None or self.play_t >= rng[1]:
                self.play_t = rng[0]
            self._play_clock = time.monotonic()
            self.play_timer.start(100)
        else:
            self.play_timer.stop()
        self.playing = on
        self._play_frame()

    def stop_playback(self):
        """Leave playback: the map shows the latest positions again."""
        self.play_timer.stop()
        self.playing = False
        if self.play_t is not None:
            self.play_t = None
            if self.win is not None:
                self._update_overlays()
        self._play_frame()

    def _play_tick(self):
        now = time.monotonic()
        dt, self._play_clock = now - self._play_clock, now
        rng = self.play_range()
        if rng is None or self.play_t is None:
            self.stop_playback()
            return
        t = self.play_t + timedelta(seconds=dt * float(self.cfg["play_speed"]))
        if t >= rng[1]:                      # reached the end: pause there
            t = rng[1]
            self.play_timer.stop()
            self.playing = False
        self.play_t = t
        self._play_frame()

    def _play_frame(self):
        if self.play_t is not None and self.win is not None:
            self._update_overlays()
        if self.dock is not None and self.dock.isVisible():
            self.dock.refresh_playback()

    def _play_times(self):
        if self._pb_src is not self.tracks:      # tracks replaced by a load / Live update
            self._pb_src = self.tracks
            self._pb_times = {k: [p["_t"].timestamp() for p in v] for k, v in self.tracks.items()}
        return self._pb_times

    def _overlay_data(self):
        """({dev_id: track fixes}, {dev_id: marker fix}) to draw for the shown devices.
        In playback: each track up to the playback time, ending at a point interpolated
        between the two fixes either side of it, which is also where the marker goes."""
        tracks = self._shown(self.tracks)
        if self.play_t is None:
            return tracks, self._shown(self.marker_pos)
        times, t = self._play_times(), self.play_t.timestamp()
        out_tracks, out_markers = {}, {}
        for dev_id, pts in tracks.items():
            ts = times.get(dev_id) or []
            i = bisect.bisect_right(ts, t)
            if i == 0 or len(ts) != len(pts):    # not started yet
                continue
            done = pts[:i]
            if i < len(pts) and t > ts[i - 1]:
                a, b = pts[i - 1], pts[i]
                f = (t - ts[i - 1]) / (ts[i] - ts[i - 1])

                def mid(key):
                    return float(a.get(key) or 0) + f * (float(b.get(key) or 0) - float(a.get(key) or 0))
                # Accuracy: the larger of the two fixes' — "the device was somewhere in here",
                # never more precise than either real fix (0 = unknown, as Traccar reports it)
                acc = max(float(a.get("accuracy") or 0), float(b.get("accuracy") or 0))
                done = done + [dict(a, latitude=mid("latitude"), longitude=mid("longitude"),
                                    altitude=mid("altitude"), speed=mid("speed"), accuracy=acc, _t=self.play_t)]
            out_tracks[dev_id] = done
            out_markers[dev_id] = done[-1]
        return out_tracks, out_markers

    def _overlay_fresh(self, t):
        """Colour (not grey) on the map: always in playback, where 'stale' means nothing."""
        return self.play_t is not None or self._fresh(t)

    # ── Drawn on the map ──────────────────────────────────────────────────
    def _apply_item_flags(self):
        it = self._item
        it.show_markers = bool(self.cfg["show_markers"])
        it.show_labels = bool(self.cfg["show_labels"])
        it.show_tracks = bool(self.cfg["show_tracks"])
        it.show_accuracy = bool(self.cfg["show_accuracy"])

    def _marker_tip(self, dev_id, p):
        attrs = p.get("attributes") or {}
        bat = attrs.get("batteryLevel")
        if self.play_t is not None:
            when = "Playback  %s" % p["_t"].astimezone().strftime("%d %b %H:%M:%S")
        else:
            when = "Fix %s ago  (%s)" % (_age_text(p["_t"]), _fmt_local(p["_t"]))
        tip = "%s\n%s\n%d km/h" % (self._name(dev_id), when, round((p.get("speed") or 0) * 1.852))
        if isinstance(bat, (int, float)):
            tip += "  ·  battery %g%%" % float(bat)
        loc = self.format_location(p)
        if loc:
            tip += "\n%s:  %s" % (self.coord_label(), loc)
        return tip

    def _update_canvas(self):
        canvas = self.iface.mapCanvas()
        if self._item is None:
            self._item = _LiveItem(canvas)
        tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())

        def T(lon, lat):
            return tr.transform(QgsPointXY(lon, lat))

        dest = canvas.mapSettings().destinationCrs()
        crs_key = dest.authid() or dest.toWkt()

        def full_line(dev_id, pts):
            """Every fix of the track in the map CRS ([QPointF]); reprojected once, until it changes."""
            c = self._line_cache.get(dev_id)
            if c is None or c[0] is not pts or c[1] != crs_key:
                g = QgsGeometry(QgsLineString([p["longitude"] for p in pts], [p["latitude"] for p in pts]))
                g.transform(tr)
                poly = g.asQPolygonF()
                # PyQt's items view the polygon's memory: keep the polygon alive alongside them
                c = (pts, crs_key, list(poly), poly)
                self._line_cache[dev_id] = c
            return c[2]

        trk, mk = self._overlay_data()
        tracks, markers = [], []
        try:
            full = self._shown(self.tracks)
            if self.play_t is not None:          # the whole track, faint, under the part played
                for dev_id, pts in full.items():
                    if len(pts) >= 2:
                        col = QColor(_device_color(self._name(dev_id)))
                        col.setAlpha(70)
                        tracks.append((col, QPolygonF(full_line(dev_id, pts))))
            for dev_id, pts in trk.items():
                if len(pts) < 2:
                    continue
                # The fixes played so far are the start of the full track: draw exactly those
                # vertices (never resampled, so the route can't change shape), plus the moving end.
                allpts = full[dev_id]
                n = len(pts)
                moving = n > len(allpts) or pts[-1] is not allpts[n - 1]
                verts = full_line(dev_id, allpts)[:n - 1 if moving else n]
                if moving:
                    q = T(pts[-1]["longitude"], pts[-1]["latitude"])
                    verts = verts + [QPointF(q.x(), q.y())]
                col = QColor(_device_color(self._name(dev_id)) if self._overlay_fresh(pts[-1]["_t"]) else "#9E9E9E")
                col.setAlpha(215)
                tracks.append((col, QPolygonF(verts)))
            for dev_id, p in mk.items():
                acc = float(p.get("accuracy") or 0)
                acc_pt = None
                if acc > 0:      # a point `acc` metres east → circle radius in any map CRS
                    dlon = acc / (111320.0 * max(0.01, math.cos(math.radians(p["latitude"]))))
                    acc_pt = T(p["longitude"] + dlon, p["latitude"])
                name = self._name(dev_id)
                markers.append({
                    "pt": T(p["longitude"], p["latitude"]), "acc_pt": acc_pt,
                    "color": QColor(_device_color(name) if self._overlay_fresh(p["_t"]) else "#9E9E9E"),
                    "label": name, "tip": self._marker_tip(dev_id, p)})
        except Exception as exc:          # e.g. a map CRS that can't take these points
            self._log("Could not draw the live view: %s" % exc, _MSG_WARN)
        self._item.tracks, self._item.markers = tracks, markers
        self._apply_item_flags()
        self._item.updatePosition()
        self._item.update()

    def marker_tip_at(self, x, y):
        """Tooltip text for a marker within 10 px of canvas pixel (x, y), else ''."""
        if self._item is None or not self.cfg["show_markers"]:
            return ""
        m2p = self.iface.mapCanvas().getCoordinateTransform()
        best_d, best_tip = 10.0, ""
        for m in self._item.markers:
            q = m2p.transform(m["pt"])
            d = math.hypot(q.x() - x, q.y() - y)
            if d <= best_d:
                best_d, best_tip = d, m["tip"]
        return best_tip

    def _on_canvas_move(self, _pt):
        if self._item is None:
            return
        canvas = self.iface.mapCanvas()
        pos = canvas.mouseLastXY()
        tip = self.marker_tip_at(pos.x(), pos.y())
        if tip:
            QToolTip.showText(canvas.mapToGlobal(pos), tip, canvas)
            self._tip_on = True
        elif self._tip_on:
            QToolTip.hideText()
            self._tip_on = False

    def _on_crs_changed(self):
        if self._item is not None and self.win is not None:
            self._update_canvas()
        if self._gf_item is not None:
            self._draw_geofences()
        if self._pending_item is not None:
            self._draw_pending()

    # ── As temporary layers ───────────────────────────────────────────────
    def _update_layers(self):
        trk, mk_data = self._overlay_data()
        mk = self._overlay("markers")
        feats = []
        for dev_id, p in mk_data.items():
            f = QgsFeature(mk.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["longitude"], p["latitude"])))
            attrs = p.get("attributes") or {}
            name = self._name(dev_id)
            f.setAttributes([dev_id, name, _qdt(p["_t"]), _local_text(p["_t"]),
                             round((p.get("speed") or 0) * 1.852, 1), attrs.get("batteryLevel"),
                             p.get("accuracy") or 0.0, 1 if self._overlay_fresh(p["_t"]) else 0,
                             _device_color(name)])
            feats.append(f)
        mk.dataProvider().truncate()
        mk.dataProvider().addFeatures(feats)
        mk.updateExtents()
        mk.triggerRepaint()

        tk = self._overlay("tracks")
        feats = []
        for dev_id, pts in trk.items():
            if len(pts) < 2:
                continue
            line = QgsLineString([p["longitude"] for p in pts], [p["latitude"] for p in pts],
                                 [float(p.get("altitude") or 0) for p in pts],
                                 [p["_t"].timestamp() for p in pts])
            name = self._name(dev_id)
            f = QgsFeature(tk.fields())
            f.setGeometry(QgsGeometry(line))
            f.setAttributes([dev_id, name, len(pts), _local_text(pts[0]["_t"]),
                             _local_text(pts[-1]["_t"]), 1 if self._overlay_fresh(pts[-1]["_t"]) else 0,
                             _device_color(name)])
            feats.append(f)
        tk.dataProvider().truncate()
        tk.dataProvider().addFeatures(feats)
        tk.updateExtents()
        tk.triggerRepaint()

    def _remove_overlays(self):
        if self._item is not None:
            try:
                self._item.scene().removeItem(self._item)
            except (RuntimeError, AttributeError):
                pass
            self._item = None
            self._line_cache = {}
            self.iface.mapCanvas().refresh()
        prj = QgsProject.instance()
        for lid in (self._mk_id, self._tk_id):
            if lid and prj.mapLayer(lid) is not None:
                prj.removeMapLayer(lid)
        self._mk_id = self._tk_id = ""
        root = prj.layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is not None and not group.children():
            root.removeChildNode(group)

    def _remove_stale_overlays(self):
        """Overlay layers saved into a project by mistake come back empty — drop them."""
        prj = QgsProject.instance()
        stale = [lid for lid, lyr in prj.mapLayers().items()
                 if str(lyr.customProperty(OVERLAY_PROP, "")) in ("1", "true", "True")
                 and lid not in (self._mk_id, self._tk_id)]
        if stale:
            prj.removeMapLayers(stale)
        root = prj.layerTreeRoot()
        group = root.findGroup(GROUP_NAME)
        if group is not None and not group.children():
            root.removeChildNode(group)

    def _on_project_changed(self, *_args):
        self._mk_id = self._tk_id = ""      # a new project has none of our Temp layers
        self._remove_stale_overlays()
        if self.win is not None:
            self._update_overlays()

    # ── Zoom ──────────────────────────────────────────────────────────────
    def _to_canvas(self):
        canvas = self.iface.mapCanvas()
        return canvas, QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(),
                                              QgsProject.instance())

    def zoom_to(self, pos):
        canvas, tr = self._to_canvas()
        canvas.setCenter(tr.transform(QgsPointXY(pos["longitude"], pos["latitude"])))
        canvas.refresh()

    def shown_pos(self, dev_id):
        """Where the device is shown on the map: its playback position, or its marker fix."""
        if self.play_t is not None:
            return self._overlay_data()[1].get(dev_id)
        return self.marker_pos.get(dev_id)

    def pan_to_shown(self, dev_id=None):
        """Pan to a device where it is shown (playback position included), or fit all shown devices."""
        if dev_id is not None:
            pos = self.shown_pos(dev_id)
            if pos is None:
                self._bar("%s isn't on the map at this time" % self._name(dev_id), _MSG_WARN, 3)
            else:
                self.zoom_to(pos)
            return
        pts = list(self._overlay_data()[1].values())
        if len(pts) == 1:
            self.zoom_to(pts[0])
        elif pts:
            canvas, tr = self._to_canvas()
            rect = tr.transformBoundingBox(QgsRectangle(
                min(p["longitude"] for p in pts), min(p["latitude"] for p in pts),
                max(p["longitude"] for p in pts), max(p["latitude"] for p in pts)))
            ext = canvas.extent()
            if rect.width() < ext.width() * 0.9 and rect.height() < ext.height() * 0.9:
                canvas.setCenter(rect.center())          # they fit: pan, keep the scale
            else:
                rect.scale(1.3)
                canvas.setExtent(rect)
            canvas.refresh()

    def zoom_to_all(self):
        pts = [p for v in self._shown(self.tracks).values() for p in v]
        pts += list(self._shown(self.marker_pos).values())
        if not pts:
            return
        canvas, tr = self._to_canvas()
        xs = [p["longitude"] for p in pts]
        ys = [p["latitude"] for p in pts]
        rect = tr.transformBoundingBox(QgsRectangle(min(xs), min(ys), max(xs), max(ys)))
        if rect.width() == 0 and rect.height() == 0:
            canvas.setCenter(rect.center())
        else:
            rect.scale(1.15)
            canvas.setExtent(rect)
        canvas.refresh()

    # ── Per device: show / hide ───────────────────────────────────────────
    def hidden_ids(self):
        """Traccar ids of the devices unticked in the list (kept between sessions)."""
        out = set()
        for x in str(self.cfg["hidden_devices"]).split(","):
            try:
                out.add(int(x))
            except ValueError:
                pass
        return out

    def _set_hidden(self, ids):
        self.set_cfg("hidden_devices", ",".join(str(i) for i in sorted(ids)))
        self._rebuild()

    def set_device_shown(self, dev_id, on):
        hidden = self.hidden_ids()
        if on:
            hidden.discard(dev_id)
        else:
            hidden.add(dev_id)
        self._set_hidden(hidden)

    def show_only(self, dev_id):
        self._set_hidden((self.hidden_ids() | set(self.devices)) - {dev_id})

    def show_all(self):
        self._set_hidden(set())

    def hide_all(self):
        self._set_hidden(self.hidden_ids() | set(self.devices))

    def _shown(self, by_dev, only=None):
        """by_dev ({dev_id: …}) limited to the devices shown — or to device `only`, shown or not."""
        if only is not None:
            return {k: v for k, v in by_dev.items() if k == only}
        hidden = self.hidden_ids()
        return {k: v for k, v in by_dev.items() if k not in hidden}

    def _none_shown_hint(self, only):
        if only is None and self.hidden_ids() & set(self.devices):
            return " (hidden devices are not saved — tick them in the list)"
        return ""

    # ── Per device: location and clipboard ─────────────────────────────────
    def set_coord_format(self, key):
        self.set_cfg("coord_format", key)
        self._rebuild()

    def _format_crs(self, key):
        if key == "project":
            return QgsProject.instance().crs()
        if key == "custom":
            return QgsCoordinateReferenceSystem(self.cfg["custom_crs"])
        return QgsCoordinateReferenceSystem({"ig": "EPSG:29903", "uk": "EPSG:27700"}.get(key, "EPSG:4326"))

    def coord_label(self, key=None):
        """'Lat / Long', 'Project CRS (EPSG:2157)', 'Irish Grid' … for format key (default: the chosen one)."""
        key = key or self.cfg["coord_format"]
        label = dict(COORD_FORMATS).get(key, key)
        if key in ("project", "custom"):
            authid = self._format_crs(key).authid()
            label += " (%s)" % (authid or "not set")
        return label

    def _transform(self, p, crs):
        """Position → QgsPointXY in crs, or None (invalid CRS / outside its area)."""
        if not crs.isValid():
            return None
        try:
            return QgsCoordinateTransform(WGS84, crs, QgsProject.instance()).transform(
                QgsPointXY(p["longitude"], p["latitude"]))
        except QgsCsException:
            return None

    def format_location(self, p, key=None):
        """A position as text in a location format; '' when it can't be expressed in it."""
        key = key or self.cfg["coord_format"]
        if key == "latlon":
            return format_latlon(p["latitude"], p["longitude"], self.cfg["latlon_style"], self.cfg["latlon_hemi"])
        crs = self._format_crs(key)
        xy = self._transform(p, crs)
        if xy is None:
            return ""
        if key == "ig":
            return irish_grid_ref(xy.x(), xy.y())
        if key == "uk":
            return uk_grid_ref(xy.x(), xy.y())
        if crs.isGeographic():
            return format_latlon(xy.y(), xy.x(), self.cfg["latlon_style"], self.cfg["latlon_hemi"])
        return "%.1f, %.1f" % (xy.x(), xy.y())

    def location_wkt(self, p, crs=WGS84):
        xy = self._transform(p, crs)
        return QgsGeometry.fromPointXY(xy).asWkt(7 if crs.isGeographic() else 2) if xy is not None else ""

    def track_wkt(self, dev_id, crs=WGS84):
        """The device's fixes in the window as a WKT LineString (a Point when there is only one)."""
        pts = [self._transform(p, crs) for p in self.tracks.get(dev_id, [])]
        if not pts or None in pts:
            return ""
        geom = QgsGeometry.fromPolylineXY(pts) if len(pts) > 1 else QgsGeometry.fromPointXY(pts[0])
        return geom.asWkt(7 if crs.isGeographic() else 2)

    def track_lines(self, dev_id):
        """One line per fix in the window: local time, then the location in the chosen format."""
        return "\n".join("%s\t%s" % (p["_t"].astimezone().strftime("%Y-%m-%d %H:%M:%S"), self.format_location(p))
                         for p in self.tracks.get(dev_id, []))

    def copy_text(self, text, what):
        if not text:
            self._bar("Nothing to copy (%s)" % what, _MSG_WARN)
            return
        QgsApplication.clipboard().setText(text)
        short = text if len(text) <= 60 else text[:57] + "…"
        self._bar("Copied %s:  %s" % (what, short.replace("\n", " ")), _MSG_OK, 3)

    # ── what3words (key in QGIS's password manager; looked up only on request) ──
    def w3w_key(self):
        if self._w3w is None:
            self._w3w = QgsApplication.authManager().authSetting(W3W_KEY_SETTING, "", True) or ""
        return str(self._w3w)

    def set_w3w_key(self, key):
        mgr = QgsApplication.authManager()
        if key:
            if not mgr.storeAuthSetting(W3W_KEY_SETTING, key, True):
                self._bar("The what3words key could not be saved in QGIS's password manager", _MSG_WARN)
        else:
            mgr.removeAuthSetting(W3W_KEY_SETTING)
        self._w3w = key

    def copy_w3w(self, p):
        """Look up the what3words address of a position and copy it ('///word.word.word')."""
        key = self.w3w_key()
        if not key:
            self._bar("Add your what3words API key in Settings → Advanced → Location", _MSG_WARN)
            self.open_settings(3)
            return
        url = "%s/convert-to-3wa?coordinates=%.6f,%.6f&language=en" % (W3W_URL, p["latitude"], p["longitude"])
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"X-Api-Key", key.encode("utf-8"))      # header, not URL: kept out of logs
        req.setRawHeader(b"Accept", b"application/json")
        try:
            req.setTransferTimeout(15000)
        except AttributeError:     # pragma: no cover
            pass
        reply = QgsNetworkAccessManager.instance().get(req)
        self._w3w_reply = reply

        def finished():
            self._w3w_reply = None
            status = reply.attribute(_HTTP_STATUS)
            data = bytes(reply.readAll())
            errstr = reply.errorString()
            reply.deleteLater()
            try:
                body = json.loads(data.decode("utf-8")) if data else {}
            except ValueError:
                body = {}
            words = body.get("words") if isinstance(body, dict) else None
            if words:
                self.copy_text("///" + words, "what3words")
                return
            err = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
            self._bar("what3words: " + (err or ("HTTP %s" % status if status else errstr)), _MSG_WARN, 8)

        reply.finished.connect(finished)

    # ── Map tools: "send" (📤 Point), "area" (⬡ Area), "line" (〰 Line), "circle" (⭕ Circle) ──
    def _canvas_menu(self, menu, event):
        """Right-click on the map: a Traccar Live submenu for the clicked spot."""
        tr = QgsCoordinateTransform(self.iface.mapCanvas().mapSettings().destinationCrs(), WGS84,
                                    QgsProject.instance())
        try:
            ll = tr.transform(event.mapPoint())
        except QgsCsException:
            return
        lat, lon = ll.y(), ll.x()
        menu.addSeparator()
        sub = menu.addMenu(QIcon(os.path.join(os.path.dirname(__file__), "icon.png")), "Traccar Live")
        sub.addAction("📤  Send point here…", lambda: self.send_point(lat, lon))
        sub.addAction("⭕  Send circle here…", lambda: self.circle_at(lat, lon))
        lyr = self.iface.activeLayer()
        if isinstance(lyr, QgsVectorLayer) and lyr.selectedFeatureCount():
            sub.addSeparator()
            sub.addAction("📑  Send %d selected feature(s) of '%s'…" % (lyr.selectedFeatureCount(), lyr.name()),
                          self.send_selected)

    # ── Map tools: "send" (📤 Point), "area" (⬡ Area), "line" (〰 Line), "circle" (⭕ Circle) ──
    # Plain menu items: choosing one starts its tool, with a hint bar and a Cancel button. Once the
    # shape is sent — or on Cancel / Esc — the map tool you had before comes back.
    TOOL_HINTS = {"send": "Click the map where the point goes",
                  "circle": "Click the map at the centre of the circle",
                  "area": "Click each corner of the area; right-click or Enter to finish, Backspace to undo",
                  "line": "Click each point of the line; right-click or Enter to finish, Backspace to undo"}

    def start_tool(self, which):
        canvas = self.iface.mapCanvas()
        current = canvas.mapTool()
        if current not in self._tools.values():
            self._tool_prev = current                  # come back to this one afterwards
        tool = self._tools.get(which)
        if tool is None:
            off, cancel = (lambda: self._tool_off(which)), (lambda: self.end_tool())   # noqa: E731
            if which in ("area", "line"):
                tool = _ShapeTool(self, canvas, which, self.shape_done, off, cancel)
            else:
                tool = _ClickTool(self, canvas, self.send_point if which == "send" else self.circle_at,
                                  off, cancel)
            self._tools[which] = tool
        canvas.setMapTool(tool)
        self._tool_which = which
        self.tool_hint(self.TOOL_HINTS[which])

    def tool_hint(self, text):
        """The bar over the map while adding: what to do, and Cancel."""
        bar = self.iface.messageBar()
        self._clear_tool_hint()
        try:
            item = bar.createMessage("Traccar Live", text)
            btn = QPushButton("Cancel")
            btn.clicked.connect(self.end_tool)
            item.layout().addWidget(btn)
            self._tool_msg = bar.pushWidget(item, _MSG_INFO, 0)
        except (AttributeError, TypeError, RuntimeError):
            self._tool_msg = None

    def _clear_tool_hint(self):
        if getattr(self, "_tool_msg", None) is not None:
            try:
                self.iface.messageBar().popWidget(self._tool_msg)
            except (RuntimeError, TypeError, AttributeError):
                pass
        self._tool_msg = None

    def end_tool(self):
        """Stop adding: the map tool from before comes back (Pan if there was none)."""
        canvas = self.iface.mapCanvas()
        tool = self._tools.get(getattr(self, "_tool_which", None))
        self._tool_which = None
        self._clear_tool_hint()
        if tool is not None and canvas.mapTool() is tool:
            canvas.unsetMapTool(tool)
            prev = getattr(self, "_tool_prev", None)
            try:
                if prev is not None:
                    canvas.setMapTool(prev)
                else:
                    self.iface.actionPan().trigger()
            except (RuntimeError, AttributeError):
                pass
        self._tool_prev = None

    def _tool_off(self, which):
        """Another map tool took over: drop the hint bar."""
        if getattr(self, "_tool_which", None) == which:
            self._tool_which = None
            self._clear_tool_hint()

    def _send_dialog(self, mode, coords, edit=None):
        """The send form (or, with edit = a geofence's JSON, the edit form); its values, or None."""
        d = SendDialog(self, mode, coords, self.iface.mainWindow(), edit)
        ok = d.exec() == _DLG_OK
        v = d.values() if ok else None
        d.deleteLater()
        return v

    def shape_done(self, kind, coords):
        """Area / Line tool finished: [(lat, lon), …] → the send form → a geofence."""
        v = self._send_dialog(kind, coords)
        if v is None:
            return                                  # corners kept: carry on, or Esc
        pts = coords + coords[:1] if kind == "area" else coords
        text = ", ".join(_ll(lat, lon) for lat, lon in pts)
        area = "POLYGON ((%s))" % text if kind == "area" else "LINESTRING (%s)" % text
        if len(area) > GEOFENCE_MAX_WKT:
            self._bar("Too many corners for Traccar (it keeps up to about 4000 characters)", _MSG_WARN, 8)
            return
        self.set_cfg("geofence_color", v["color"])
        self._upsert_geofences([(v["name"], area, False)], v["color"], geofence_extra(v))
        tool = self._tools.get(kind)
        if tool is not None:
            tool.reset()
        self.end_tool()

    def send_point(self, lat, lon):
        """Ask for a name (and details), then send lat / lon (WGS84) as a fix of that device, time = now.
        An existing device (by name or identifier) moves there; a new name creates a new device."""
        base = osmand_url(self.cfg)
        if not base:
            self._bar("Set up the connection first (Settings… → Connection)", _MSG_WARN)
            return
        v = self._send_dialog("point", [(lat, lon)])
        if v is None:
            return                                  # still adding: click again, or Cancel / Esc
        self.send_point_to(v, lat, lon)
        self.end_tool()

    def send_point_to(self, v, lat, lon):
        """Send-dialog values v → a fix at lat / lon of the device named v["name"] (added if new)."""
        base = osmand_url(self.cfg)
        where = self.format_location({"latitude": lat, "longitude": lon}) or format_latlon(lat, lon)
        name = v["name"]
        self._send_name = name
        t = int(time.time())
        key = self._add_pending(name, lat, lon)

        def failed(msg):
            self._drop_pending(key)
            self._bar(msg, _MSG_WARN, 12)

        def got_devices(devs):
            """OsmAnd needs the device's Identifier (uniqueId), not its name: accept either."""
            uid = next((d.get("uniqueId") for d in devs if d.get("uniqueId") == name), None)
            if uid is None:
                uid = next((d.get("uniqueId") for d in devs
                            if (d.get("name") or "").strip().lower() == name.lower()), None)
            if uid is not None:
                self._send_fix(base, uid, name, lat, lon, t, where, key)
                return
            new_uid = v.get("unique_id") or SENT_POINT_PREFIX + "%s-%d" % (
                re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "point", t)
            body = {"name": name, "uniqueId": new_uid, "category": v.get("category") or "default"}
            if v.get("group_id"):
                body["groupId"] = v["group_id"]
            for field in ("phone", "model", "contact"):
                if v.get(field):
                    body[field] = v[field]
            self.api.post("/api/devices", body,
                          lambda d: self._send_fix(base, d.get("uniqueId") or new_uid, name, lat, lon, t, where, key),
                          lambda m: failed("Couldn't add '%s' to Traccar (%s) — your account may not be "
                                           "allowed to add devices." % (name, m)))

        self.api.get("/api/devices", got_devices,
                     lambda m: failed("Send point needs your Traccar login to find or add '%s': %s" % (name, m)))

    # ── Points being sent: shown at once, until a refresh after Traccar accepted them ──
    def _add_pending(self, name, lat, lon):
        self._pending_seq = getattr(self, "_pending_seq", 0) + 1
        self.pending_points.append({"key": self._pending_seq, "name": name, "lat": lat, "lon": lon,
                                    "confirmed": None})
        self._draw_pending()
        return self._pending_seq

    def _drop_pending(self, key):
        self.pending_points = [m for m in self.pending_points if m["key"] != key]
        self._draw_pending()

    def _confirm_pending(self, key):
        for m in self.pending_points:
            if m["key"] == key:
                m["confirmed"] = time.time()
                m["tries"] = 0
        if not self.pending_timer.isActive():
            self.pending_timer.start()

    def _pending_tick(self):
        """Every 2 s after Traccar accepted a point: refresh until the real device is shown (the
        refresh drops the stand-in). In a fixed past window, or with nothing loaded, it can't be
        shown: drop it now. Gives up after 5 tries."""
        done = [m for m in self.pending_points if m["confirmed"] is not None]
        if not done:
            self.pending_timer.stop()
            return
        if self.win is None or not self.win["moving"]:
            for m in done:
                self._drop_pending(m["key"])
            self._bar("Sent to Traccar — not shown here: the time window ends in the past (or nothing is "
                      "loaded). Choose a 'Last …' window to see it.", _MSG_INFO, 6)
            self.pending_timer.stop()
            return
        for m in done:
            m["tries"] += 1
            if m["tries"] > 5:
                self._drop_pending(m["key"])
        if not self.loading:
            self.poll_live()

    def _settle_pending(self, fetch_started):
        """A refresh that started after Traccar accepted a point shows the real device: drop the stand-in."""
        keep = [m for m in self.pending_points if m["confirmed"] is None or m["confirmed"] > fetch_started]
        if len(keep) != len(self.pending_points):
            self.pending_points = keep
            self._draw_pending()

    def _draw_pending(self):
        canvas = self.iface.mapCanvas()
        if not self.pending_points:
            if self._pending_item is not None:
                try:
                    self._pending_item.scene().removeItem(self._pending_item)
                except (RuntimeError, AttributeError):
                    pass
                self._pending_item = None
                canvas.refresh()
            return
        if self._pending_item is None:
            self._pending_item = _PendingItem(canvas)
        tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        pts = []
        for m in self.pending_points:
            try:
                pt = tr.transform(QgsPointXY(m["lon"], m["lat"]))
            except QgsCsException:
                continue
            pts.append({"pt": pt, "label": "%s (sending…)" % m["name"], "color": _device_color(m["name"])})
        self._pending_item.points = pts
        self._pending_item.updatePosition()
        self._pending_item.update()

    # ── Geofences (areas / lines / circles in Traccar) ────────────────────
    def load_geofences(self):
        if not self.cfg["username"]:
            return

        def ok(gfs):
            self.geofences = []
            for g in gfs:
                attrs = g.get("attributes") or {}
                color = QColor(str(attrs.get("color") or ""))
                parsed = parse_geofence_area(g.get("area") or "")
                self.geofences.append({
                    "id": g["id"], "name": g.get("name") or "", "area": g.get("area") or "",
                    "mine": bool(attrs.get(GEOFENCE_MARK)), "raw": g,
                    "color": color.name().upper() if color.isValid() else GEOFENCE_COLOR,
                    "kind": parsed[0] if parsed else "", "description": g.get("description") or "",
                    "speed_kmh": round(float(attrs["speedLimit"]) * 1.852) if attrs.get("speedLimit") else 0})
            self.geofences.sort(key=lambda g: g["name"].lower())
            self._draw_geofences()

        def keep(key):
            def done(items):
                setattr(self, key, sorted(items or [], key=lambda x: (x.get("name") or "").lower()))
            return done

        self.api.get("/api/geofences", ok, lambda m: self._log("Geofences: " + m, _MSG_WARN))
        # For the send form; an account without access just gets empty lists
        self.api.get("/api/groups", keep("groups"), lambda _m: None)
        self.api.get("/api/calendars", keep("calendars"), lambda _m: None)

    def hidden_geofence_ids(self):
        """Traccar ids of the geofences unticked in the list (kept between sessions)."""
        out = set()
        for x in str(self.cfg["hidden_geofences"]).split(","):
            try:
                out.add(int(x))
            except ValueError:
                pass
        return out

    def set_geofence_shown(self, gid, on):
        hidden = self.hidden_geofence_ids()
        if on:
            hidden.discard(gid)
        else:
            hidden.add(gid)
        self.set_cfg("hidden_geofences", ",".join(str(i) for i in sorted(hidden)))
        self._draw_geofences()

    def set_all_geofences_shown(self, on):
        self.set_cfg("hidden_geofences", "" if on else ",".join(str(g["id"]) for g in self.geofences))
        self._draw_geofences()

    def _geofence(self, gid):
        return next((g for g in self.geofences if g["id"] == gid), None)

    def geofence_wkt(self, gid):
        """Standard WKT (longitude first, WGS84); a circle as its 72-sided polygon."""
        g = self._geofence(gid)
        parsed = parse_geofence_area(g["area"]) if g else None
        if not parsed or not parsed[1]:
            return ""
        coords = ", ".join("%.6f %.6f" % xy for xy in parsed[1])
        return "LINESTRING (%s)" % coords if parsed[0] == "line" else "POLYGON ((%s))" % coords

    def geofence_type(self, g):
        """'Area', 'Line' or 'Circle 500 m' for the list."""
        if g["kind"] == "circle":
            m = re.search(r",\s*%s\s*\)" % _NUM, g["area"])
            return "Circle %d m" % round(float(m.group(1))) if m else "Circle"
        return {"line": "Line", "polygon": "Area"}.get(g["kind"], "?")

    def _draw_geofences(self):
        if self.dock is not None:
            self.dock.refresh_geofences()
        canvas = self.iface.mapCanvas()
        hidden = self.hidden_geofence_ids()
        if not self.cfg["show_geofences"] or not any(g["id"] not in hidden for g in self.geofences):
            self._remove_geofence_item()
            return
        if self._gf_item is None:
            self._gf_item = _GeofenceItem(canvas)
        tr = QgsCoordinateTransform(WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        shapes = []
        for g in self.geofences:
            if g["id"] in hidden:
                continue
            parsed = parse_geofence_area(g["area"])
            if not parsed or not parsed[1]:
                continue
            kind, lonlat = parsed
            try:
                pts = [tr.transform(QgsPointXY(lon, lat)) for lon, lat in lonlat]
            except QgsCsException:
                continue
            if kind == "line":
                label_pt = pts[len(pts) // 2]
            else:
                inner = QgsGeometry.fromPolygonXY([pts]).pointOnSurface()
                label_pt = inner.asPoint() if not inner.isEmpty() else pts[0]
            shapes.append({"poly": QPolygonF([QPointF(p.x(), p.y()) for p in pts]),
                           "closed": kind != "line", "label": g["name"], "label_pt": label_pt,
                           "color": g["color"]})
        self._gf_item.shapes = shapes
        self._gf_item.show_labels = bool(self.cfg["show_labels"])
        self._gf_item.updatePosition()
        self._gf_item.update()

    def _remove_geofence_item(self):
        if self._gf_item is not None:
            try:
                self._gf_item.scene().removeItem(self._gf_item)
            except (RuntimeError, AttributeError):
                pass
            self._gf_item = None
            self.iface.mapCanvas().refresh()

    def _show_pending(self, items, color, extra, attrs):
        """Draw geofences being sent straight away (marked 'sending…'), so a shape doesn't vanish
        between sending it and Traccar's reply. The reload after sending replaces them with
        Traccar's copies — or removes them if Traccar refused."""
        names = {n.strip().lower() for n, _a, _s in items}
        keep = [g for g in self.geofences if not (g["mine"] and g["name"].strip().lower() in names)]
        for k, (name, area, _simp) in enumerate(items, 1):
            parsed = parse_geofence_area(area)
            speed = attrs.get("speedLimit")
            keep.append({"id": -k, "name": name, "area": area, "mine": True, "pending": True,
                         "raw": dict(extra, name=name, area=area, attributes=dict(attrs)),
                         "color": QColor(color).name().upper(), "kind": parsed[0] if parsed else "",
                         "description": extra.get("description", ""),
                         "speed_kmh": round(float(speed) * 1.852) if speed else 0})
        self.geofences = sorted(keep, key=lambda g: g["name"].lower())
        self._draw_geofences()

    def _upsert_geofences(self, items, color, extra=None):
        """items: [(name, area, simplified)], all given colour ("#RRGGBB", Traccar's 'color'
        attribute) and the extra fields from the send form ({description, calendarId, attributes}).
        A geofence sent from QGIS or QField before under the same name is replaced (PUT);
        anything else is added (POST). Then the map is redrawn."""
        extra = dict(extra or {})
        attrs = dict(extra.pop("attributes", {}), **{GEOFENCE_MARK: True, "color": color})
        self._show_pending(items, color, extra, attrs)

        def got(existing):
            mine = {(g.get("name") or "").strip().lower(): g for g in existing
                    if (g.get("attributes") or {}).get(GEOFENCE_MARK)}
            st = {"left": len(items), "added": 0, "replaced": 0, "errors": []}
            simplified = any(s for _n, _a, s in items)

            def done():
                st["left"] -= 1
                if st["left"] > 0:
                    return
                n = st["added"] + st["replaced"]
                msg = "Sent %d geofence(s) to Traccar" % n
                if st["replaced"]:
                    msg += " (%d replaced)" % st["replaced"]
                if simplified:
                    msg += " — some shapes were simplified to fit Traccar's size limit"
                if st["errors"]:
                    self._bar("%s; %d failed: %s" % (msg, len(st["errors"]), st["errors"][0]), _MSG_WARN, 12)
                else:
                    self._bar(msg, _MSG_OK, 5)
                self.load_geofences()

            for name, area, _simp in items:
                old = mine.get(name.strip().lower())

                def ok(_g, key="replaced" if old else "added"):
                    st[key] += 1
                    done()

                def bad(m, name=name):
                    st["errors"].append("%s: %s" % (name, m))
                    done()

                if old:
                    body = dict(old, name=name, area=area, **extra)
                    body["attributes"] = dict(old.get("attributes") or {}, **attrs)
                    self.api.put("/api/geofences/%d" % old["id"], body, ok, bad)
                else:
                    self.api.post("/api/geofences", dict(extra, name=name, area=area, attributes=attrs), ok, bad)

        self.api.get("/api/geofences", got,
                     lambda m: self._bar("Sending geofences needs your Traccar login: %s" % m, _MSG_WARN, 10))

    def send_selected(self):
        """The active layer's selected features → geofences (polygons, lines; points as circles
        or as points)."""
        lyr = self.iface.activeLayer()
        if not isinstance(lyr, QgsVectorLayer) or not lyr.selectedFeatureCount():
            self._bar("Select the features to send first (in the active layer of the Layers panel)", _MSG_WARN)
            return
        n = lyr.selectedFeatureCount()
        is_point = lyr.geometryType() == _GEOM_POINT
        d = QDialog(self.iface.mainWindow())
        d.setWindowTitle("Traccar Live – Send selected features")
        f = QFormLayout(d)
        head = QLabel("Send selected features")
        head.setStyleSheet("font-weight:bold;font-size:14px;")
        f.addRow(head)
        f.addRow(_hint("%d selected feature(s) of '%s'. Areas and lines become geofences in Traccar; "
                       "points become circles or points (devices)." % (n, lyr.name())))
        fld = QgsFieldComboBox()
        fld.setLayer(lyr)
        fld.setAllowEmptyFieldName(True)
        names = {nm.lower(): nm for nm in lyr.fields().names()}
        fld.setField(next((names[k] for k in ("name", "title", "sector", "sector_id", "incident_ref")
                           if k in names), ""))
        txt = QLineEdit()
        txt.setPlaceholderText("the layer's name (numbered if several)")
        f.addRow("Name from field:", fld)
        f.addRow("… or name:", txt)
        desc = QgsFieldComboBox()
        desc.setLayer(lyr)
        desc.setAllowEmptyFieldName(True)
        desc.setField("")
        f.addRow("Description from field:", desc)
        as_circles = QRadioButton("As circles (geofences)")
        as_points = QRadioButton("As points (devices)")
        as_circles.setChecked(True)
        rad = QSpinBox()
        rad.setRange(10, 100000)
        rad.setSingleStep(50)
        rad.setSuffix(" m")
        rad.setValue(int(self.cfg["circle_radius"]))
        if is_point:
            f.addRow(as_circles)
            f.addRow(as_points)
            f.addRow("Circle radius:", rad)
            as_points.toggled.connect(lambda on: rad.setEnabled(not on))
        swatches = _SwatchRow(self.cfg["geofence_color"])
        f.addRow("Colour:", swatches)
        f.addRow(_hint("A geofence sent from QGIS or QField before with the same name is replaced."))
        bb = QDialogButtonBox(_BTN_CANCEL)
        send = bb.addButton("Send to Traccar", _e(QDialogButtonBox, "ButtonRole", "AcceptRole"))
        send.setDefault(True)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        accepted = d.exec() == _DLG_OK
        field, text, radius, color = fld.currentField(), txt.text().strip(), rad.value(), swatches.color()
        desc_field, points = desc.currentField(), is_point and as_points.isChecked()
        d.deleteLater()
        if not accepted:
            return
        self.set_cfg("geofence_color", color)
        if is_point and not points:
            self.set_cfg("circle_radius", radius)
        by_desc, skipped, sent_points = {}, 0, 0
        to_wgs = QgsCoordinateTransform(lyr.crs(), WGS84, QgsProject.instance())
        for i, feat in enumerate(lyr.selectedFeatures(), 1):
            name = ""
            if field:
                v = feat[field]
                name = "" if _is_null(v) else str(v).strip()
            if not name:
                name = (text or lyr.name()) if n == 1 else "%s %d" % (text or lyr.name(), i)
            dv = feat[desc_field] if desc_field else None
            description = "" if _is_null(dv) else str(dv).strip()
            if points:
                g = QgsGeometry(feat.geometry())
                pts = (g.asMultiPoint() if g.isMultipart() else [g.asPoint()]) if not g.isEmpty() else []
                for j, p in enumerate(pts, 1):
                    try:
                        ll = to_wgs.transform(p)
                    except QgsCsException:
                        continue
                    self.send_point_to({"name": name if len(pts) == 1 else "%s (%d)" % (name, j)}, ll.y(), ll.x())
                    sent_points += 1
                if not pts:
                    skipped += 1
                continue
            try:
                areas = geofence_areas(feat.geometry(), lyr.crs(), radius)
            except QgsCsException:
                areas = []
            if not areas:
                skipped += 1
            for j, (area, simp) in enumerate(areas, 1):
                by_desc.setdefault(description, []).append(
                    (name if len(areas) == 1 else "%s (%d)" % (name, j), area, simp))
        if skipped:
            self._bar("%d feature(s) skipped: no geometry Traccar can take" % skipped, _MSG_WARN)
        for description, items in by_desc.items():
            self._upsert_geofences(items, color, {"description": description} if description else None)

    def circle_at(self, lat, lon):
        """⭕ Circle tool: the send form (name, radius, details, colour), then a circle geofence."""
        v = self._send_dialog("circle", [(lat, lon)])
        if v is None:
            return
        self.set_cfg("circle_radius", v["radius"])
        self.set_cfg("geofence_color", v["color"])
        self._upsert_geofences([(v["name"], "CIRCLE (%s, %.1f)" % (_ll(lat, lon), v["radius"]), False)],
                               v["color"], geofence_extra(v))
        self.end_tool()

    def delete_geofence(self, gid, name):
        """Ask, then delete a geofence from Traccar (one made in Traccar itself gets a firmer warning)."""
        g = self._geofence(gid)
        if g is None or g.get("pending"):        # not in Traccar yet
            return
        yes = _e(QMessageBox, "StandardButton", "Yes")
        no = _e(QMessageBox, "StandardButton", "No")
        made = ("" if g["mine"] else
                "It was made in Traccar, not sent from QGIS or QField — Traccar alerts may use it.\n\n")
        if QMessageBox.question(self.iface.mainWindow(), "Traccar Live – Delete geofence",
                                "Delete the geofence '%s' from Traccar?\n\n%sIt disappears for everyone. "
                                "This can't be undone." % (name, made), yes | no, no) != yes:
            return

        def done(_none):
            self.geofences = [g for g in self.geofences if g["id"] != gid]
            self._draw_geofences()
            self._bar("Deleted the geofence '%s' from Traccar" % name, _MSG_OK, 4)

        self.api.delete("/api/geofences/%d" % gid, done,
                        lambda m: self._bar("Couldn't delete '%s' from Traccar: %s" % (name, m), _MSG_WARN, 10))

    def set_geofence_color(self, gid, color=None):
        """Save a colour on the geofence in Traccar (its 'color' attribute); None = pick any colour."""
        g = self._geofence(gid)
        if g is None or g.get("pending"):        # not in Traccar yet
            return
        if color is None:
            picked = QColorDialog.getColor(QColor(g["color"]), self.iface.mainWindow(),
                                           "Traccar Live – Colour of '%s'" % g["name"])
            if not picked.isValid():
                return
            color = picked.name().upper()
        raw = g["raw"]
        body = dict(raw, attributes=dict(raw.get("attributes") or {}, color=color))

        def done(_g):
            self._bar("Colour of '%s' saved in Traccar" % g["name"], _MSG_OK, 3)
            self.load_geofences()

        self.api.put("/api/geofences/%d" % gid, body, done,
                     lambda m: self._bar("Couldn't change '%s' in Traccar: %s" % (g["name"], m), _MSG_WARN, 10))

    def edit_geofence(self, gid):
        """The send form, filled in with the geofence's values; Save puts the changes in Traccar."""
        g = self._geofence(gid)
        if g is None or g.get("pending"):        # not in Traccar yet
            return
        raw = g["raw"]
        mode = {"polygon": "area", "line": "line", "circle": "circle"}.get(g["kind"], "area")
        m = re.match(r"\s*CIRCLE\s*\(\s*%s\s+%s" % (_NUM, _NUM), g["area"], re.I)
        coords = [(float(m.group(1)), float(m.group(2)))] if m else []
        if not coords:
            parsed = parse_geofence_area(g["area"])
            coords = [(lat, lon) for lon, lat in (parsed[1] if parsed else [])]
        v = self._send_dialog(mode, coords, raw)
        if v is None:
            return
        extra = geofence_extra(v)
        attrs = dict(raw.get("attributes") or {})
        attrs.pop("speedLimit", None)
        attrs.pop("polylineDistance", None)
        attrs.update(extra["attributes"])
        body = dict(raw, name=v["name"], description=extra["description"], calendarId=extra["calendarId"],
                    attributes=attrs)
        if mode == "circle" and coords:
            body["area"] = "CIRCLE (%s, %.1f)" % (_ll(*coords[0]), v["radius"])

        def done(_g):
            self._bar("'%s' saved in Traccar" % v["name"], _MSG_OK, 3)
            self.load_geofences()

        self.api.put("/api/geofences/%d" % gid, body, done,
                     lambda m: self._bar("Couldn't change '%s' in Traccar: %s" % (g["name"], m), _MSG_WARN, 10))

    def zoom_to_geofence(self, gid):
        g = self._geofence(gid)
        parsed = parse_geofence_area(g["area"]) if g else None
        if not parsed or not parsed[1]:
            return
        if gid in self.hidden_geofence_ids():
            self.set_geofence_shown(gid, True)
        canvas, tr = self._to_canvas()
        xs = [lon for lon, _lat in parsed[1]]
        ys = [lat for _lon, lat in parsed[1]]
        try:
            rect = tr.transformBoundingBox(QgsRectangle(min(xs), min(ys), max(xs), max(ys)))
        except QgsCsException:
            return
        rect.scale(1.3)
        canvas.setExtent(rect)
        canvas.refresh()

    def is_sent_point(self, dev_id):
        """A device added by Send point (identifier qgis-…) — the only kind the plugin deletes."""
        return self.devices.get(dev_id, {}).get("uniqueId", "").startswith(SENT_POINT_PREFIX)

    def delete_sent_point(self, dev_id):
        """Ask, then delete a Send point device (and its positions) from Traccar."""
        if not self.is_sent_point(dev_id):
            return
        name = self._name(dev_id)
        yes = _e(QMessageBox, "StandardButton", "Yes")
        no = _e(QMessageBox, "StandardButton", "No")
        if QMessageBox.question(self.iface.mainWindow(), "Traccar Live – Delete point",
                                "Delete '%s' from Traccar?\n\nIt disappears for everyone, with all its "
                                "positions. This can't be undone." % name, yes | no, no) != yes:
            return

        def done(_none):
            self.devices.pop(dev_id, None)
            for d in (self.tracks, self.latest, self.marker_pos, self._line_cache):
                d.pop(dev_id, None)
            self._rebuild()
            self._bar("Deleted '%s' from Traccar" % name, _MSG_OK, 4)

        self.api.delete("/api/devices/%d" % dev_id, done,
                        lambda m: self._bar("Couldn't delete '%s' from Traccar: %s" % (name, m), _MSG_WARN, 10))

    def _send_fix(self, base, dev, name, lat, lon, t, where, key=None):
        """OsmAnd fix for the device with identifier dev (shown to the user as name); key: its
        stand-in marker (kept until a refresh after this fix, dropped if it fails)."""
        url = "%s%sid=%s&lat=%.7f&lon=%.7f&timestamp=%d&valid=true" % (
            base, "&" if "?" in base else "?", _quote(dev, safe=""), lat, lon, t)
        req = QNetworkRequest(QUrl(url))
        try:
            req.setTransferTimeout(15000)
        except AttributeError:     # pragma: no cover
            pass
        reply = QgsNetworkAccessManager.instance().get(req)
        self._send_replies.add(reply)

        def finished():
            self._send_replies.discard(reply)
            status = reply.attribute(_HTTP_STATUS)
            err, errstr = reply.error(), reply.errorString()
            reply.deleteLater()
            if err == _NET_OK and (status is None or int(status) == 200):
                self._bar("Sent to Traccar as '%s':  %s" % (name, where), _MSG_OK, 4)
                self._confirm_pending(key)               # refreshes until it shows, Live on or off
                return
            self._drop_pending(key)
            if status and int(status) == 400:
                self._bar("Traccar refused the point for '%s' (HTTP 400, identifier '%s') — check the "
                          "device isn't disabled, and that 'Send to' is the same Traccar server as the "
                          "Server URL." % (name, dev), _MSG_WARN, 10)
            elif status:
                self._bar("Send point: HTTP %d from %s" % (int(status), base), _MSG_WARN, 8)
            else:
                self._bar("Send point: no response from %s — check 'Send to' in Settings → Connection "
                          "(%s)" % (base, errstr), _MSG_WARN, 10)

        reply.finished.connect(finished)

    # ── Per device: save to file ──────────────────────────────────────────
    EXPORT_FILTERS = {"gpx": "GPX (*.gpx)", "gpkg": "GeoPackage (*.gpkg)", "csv": "CSV (*.csv)"}

    def save_device_file(self, dev_id):
        """Ask for a file name, then export the device's fixes in the time window."""
        if self.win is None:
            self._bar("Nothing loaded yet — click ↻ Refresh or ▶ Live", _MSG_WARN)
            return
        name = self._name(dev_id)
        s = QSettings()
        folder = (s.value(SETTINGS_NS + "/last_export_dir", "", type=str)
                  or QgsProject.instance().homePath() or os.path.expanduser("~"))
        ext = s.value(SETTINGS_NS + "/last_export_ext", "gpx", type=str)
        if ext not in self.EXPORT_FILTERS:
            ext = "gpx"
        base = re.sub(r"[^\w\-]+", "_", name).strip("_") or "device"
        suggested = os.path.join(folder, "%s_%s.%s" % (base, self.win["to"].astimezone().strftime("%Y%m%d_%H%M"), ext))
        path, chosen = QFileDialog.getSaveFileName(
            self.iface.mainWindow(), "Save %s to file" % name, suggested,
            ";;".join(self.EXPORT_FILTERS.values()), self.EXPORT_FILTERS[ext])
        if not path:
            return
        given = os.path.splitext(path)[1].lower().lstrip(".")
        if given in self.EXPORT_FILTERS:
            ext = given
        else:
            ext = next((k for k, v in self.EXPORT_FILTERS.items() if v == chosen), ext)
            path += "." + ext
        s.setValue(SETTINGS_NS + "/last_export_dir", os.path.dirname(path))
        s.setValue(SETTINGS_NS + "/last_export_ext", ext)
        ok, msg = self.export_device(dev_id, path)
        self._bar(msg, _MSG_OK if ok else _MSG_WARN)

    def export_device(self, dev_id, path):
        """Write one device's fixes in the time window to a .gpx, .gpkg or .csv file.
        Returns (ok, message). With no fixes in the window, its last known fix is written."""
        name = self._name(dev_id)
        fixes = list(self.tracks.get(dev_id, []))
        last = self.marker_pos.get(dev_id)
        if self.win is None or (not fixes and last is None):
            return False, "No fixes for %s in this window" % name
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".gpkg":
                err = self._export_gpkg(dev_id, path, fixes or [last])
                if err:
                    return False, "Could not save %s: %s" % (os.path.basename(path), err)
            elif ext == ".csv":
                self._export_csv(dev_id, path, fixes or [last])
            else:
                self._export_gpx(dev_id, path, fixes, last)
        except OSError as exc:
            return False, "Could not write %s: %s" % (path, exc)
        return True, "Saved %s — %d fix(es) — to %s" % (name, len(fixes or [last]), os.path.basename(path))

    def _export_gpx(self, dev_id, path, fixes, last):
        """GPX 1.1: a waypoint at the marker's fix, and a track of the fixes in the window."""
        name = _xml(self._name(dev_id))

        def pt(tag, p, extra=""):
            return '<%s lat="%.7f" lon="%.7f"><ele>%.1f</ele><time>%s</time>%s</%s>' % (
                tag, p["latitude"], p["longitude"], float(p.get("altitude") or 0), _iso(p["_t"]), extra, tag)

        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<gpx version="1.1" creator="Traccar Live (QGIS)" xmlns="http://www.topografix.com/GPX/1/1">',
               "<metadata><name>%s</name><time>%s</time></metadata>" % (name, _iso(datetime.now(timezone.utc)))]
        if last is not None:
            out.append(pt("wpt", last, "<name>%s</name><desc>Last fix %s</desc>" % (
                name, _xml(_local_text(last["_t"])))))
        if fixes:
            out.append("<trk><name>%s</name><desc>%s</desc><trkseg>" % (
                name, _xml(_span_text(self.win["frm"], self.win["to"]))))
            out.extend("  " + pt("trkpt", p) for p in fixes)
            out.append("</trkseg></trk>")
        out.append("</gpx>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")

    def _export_csv(self, dev_id, path, fixes):
        """One row per fix; utf-8 with BOM so Excel reads accented names."""
        name = self._name(dev_id)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["device_id", "name", "fix_time", "fix_local", "latitude", "longitude",
                        "altitude_m", "speed_kmh", "course", "accuracy_m", "battery", "motion", "address"])
            for p in fixes:
                attrs = p.get("attributes") or {}
                w.writerow([dev_id, name, _iso(p["_t"]), _local_text(p["_t"]), p["latitude"], p["longitude"],
                            p.get("altitude") or 0, round((p.get("speed") or 0) * 1.852, 1),
                            p.get("course") or 0, p.get("accuracy") or 0, attrs.get("batteryLevel"),
                            attrs.get("motion"), p.get("address") or ""])

    def _export_gpkg(self, dev_id, path, fixes):
        """traccar_points (every fix) + traccar_tracks (one line), the shared schema. '' or an error."""
        pts, trk, errors = create_template_gpkg(path)
        if errors:
            return "; ".join(errors)
        if self._write_points(pts, fixes)[0] < 0:
            return "positions could not be written"
        if self.tracks.get(dev_id) and self._write_tracks(trk, {dev_id: fixes})[0] < 0:
            return "track could not be written"
        return ""

    # ════════════════════════════════════════════════════════════════════════
    #  Save — writes exactly what the time window shows
    # ════════════════════════════════════════════════════════════════════════

    def saves_to_html(self):
        prj = QgsProject.instance()
        pt = prj.mapLayer(self.cfg["points_layer_id"]) if self.cfg["points_layer_id"] else None
        ln = prj.mapLayer(self.cfg["tracks_layer_id"]) if self.cfg["tracks_layer_id"] else None
        if pt is None and ln is None:
            return "<a href='#'>⚠ No layers chosen to save to — set them up</a>"
        parts = []
        if pt is not None:
            parts.append("Positions → <b>%s</b> (%s)" % (
                pt.name(), "every fix" if self.cfg["points_mode"] == 1 else "latest fix"))
        if ln is not None:
            parts.append("Tracks → <b>%s</b> (%s)" % (
                ln.name(), "keep most recent" if self.cfg["tracks_mode"] == 1 else "add new"))
        return " · ".join(parts) + "  <a href='#'>change…</a>"

    def _target(self, key, what):
        lid = self.cfg[key]
        lyr = QgsProject.instance().mapLayer(lid) if lid else None
        if lyr is None:
            self._bar("Choose a layer for %s first" % what, _MSG_WARN)
            self.open_settings(1)
            return None
        if self.win is None:
            self._bar("Nothing loaded yet — click ↻ Refresh or ▶ Live", _MSG_WARN)
            return None
        return lyr

    def save_positions(self, only=None):
        """Save the devices shown on the map, or just device `only`."""
        lyr = self._target("points_layer_id", "positions")
        if lyr is None:
            return
        if self.cfg["points_mode"] == 1:
            positions = [p for v in self._shown(self.tracks, only).values() for p in v]
        else:
            positions = list(self._shown(self.marker_pos, only).values())
        if not positions:
            self._bar("No positions to save" + self._none_shown_hint(only), _MSG_WARN)
            return
        n, note = self._write_points(lyr, positions)
        if n >= 0:
            self._bar("Saved %d point(s) to %s%s" % (n, lyr.name(), note), _MSG_OK)

    def save_tracks(self, only=None):
        """Save the devices shown on the map, or just device `only`."""
        lyr = self._target("tracks_layer_id", "tracks")
        if lyr is None:
            return
        by_dev = {k: v for k, v in self._shown(self.tracks, only).items() if v}
        if not by_dev:
            self._bar("No fixes in this window to save" + self._none_shown_hint(only), _MSG_WARN)
            return
        n, note = self._write_tracks(lyr, by_dev)
        if n >= 0:
            self._bar("Saved %d track(s) to %s%s" % (n, lyr.name(), note), _MSG_OK)

    # ── Field helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _field_kind(field):
        tn = field.typeName().lower()
        if "date" in tn or "time" in tn:
            return "dt"
        if field.isNumeric():
            return "num"
        return "text"

    def _attrs(self, fields, vals, tag):
        """{field index: value}, converted to each field's type; tag beats other values."""
        tag_text = tag or self.cfg["tag_text"]
        tag_field = self.cfg["tag_field"] if (self.cfg["tag_enabled"] and tag_text) else ""
        out = {}
        for i, field in enumerate(fields):
            name = field.name()
            if name == tag_field:
                out[i] = tag_text
                continue
            if name not in vals or vals[name] is None:
                continue
            v, kind = vals[name], self._field_kind(field)
            if isinstance(v, datetime):
                v = _qdt(v) if kind == "dt" else _iso(v)
            elif kind == "text" and not isinstance(v, str):
                v = str(v)
            elif kind == "num" and isinstance(v, str):
                continue
            out[i] = v
        return out

    @staticmethod
    def _name_field(names, chosen):
        if chosen and chosen in names:
            return chosen
        return "name" if "name" in names else ""

    @staticmethod
    def _commit(lyr, feats, delete_ids):
        """Write in the layer's edit session; returns (count, note) or (-1, '')."""
        was_editing = lyr.isEditable()
        if not was_editing and not lyr.startEditing():
            return -1, ""
        if delete_ids:
            lyr.deleteFeatures(delete_ids)
        # Match the layer's geometry type: drop Z / M, single → multi, etc.
        feats = QgsVectorLayerUtils.makeFeaturesCompatible(feats, lyr)
        res = lyr.addFeatures(feats)
        if not (res[0] if isinstance(res, tuple) else res):
            if not was_editing:
                lyr.rollBack()
            return -1, ""
        if not was_editing:
            if not lyr.commitChanges():
                errs = "; ".join(lyr.commitErrors())
                lyr.rollBack()
                QgsMessageLog.logMessage(errs, "Traccar Live", _MSG_WARN)
                return -1, ""
        lyr.triggerRepaint()
        return len(feats), (" (added to the open edit session — save edits to keep them)"
                            if was_editing else "")

    def _write_points(self, lyr, positions):
        fields = lyr.fields()
        names = fields.names()
        name_field = self._name_field(names, self.cfg["points_name_field"])
        tr = QgsCoordinateTransform(WGS84, lyr.crs(), QgsProject.instance())
        has_z = QgsWkbTypes.hasZ(lyr.wkbType())
        saved = datetime.now(timezone.utc)
        tag = self.win["tag"] if self.win else ""
        feats = []
        for p in positions:
            dev_id = p.get("deviceId")
            info = self.devices.get(dev_id, {})
            attrs = p.get("attributes") or {}
            vals = {
                "device_id": dev_id, "name": self._name(dev_id), "status": info.get("status", ""),
                "fix_time": p["_t"], "fix_local": _local_text(p["_t"]),
                "speed_kmh": round((p.get("speed") or 0) * 1.852, 1),
                "course": p.get("course") or 0.0, "altitude_m": p.get("altitude") or 0.0,
                "accuracy_m": p.get("accuracy") or 0.0, "battery": attrs.get("batteryLevel"),
                "address": p.get("address") or "", "motion": str(attrs.get("motion", "")),
                "fetched_at": saved,
            }
            if name_field:
                vals[name_field] = vals["name"]
            xy = tr.transform(QgsPointXY(p["longitude"], p["latitude"]))
            geom = QgsGeometry(QgsPoint(xy.x(), xy.y(), float(p.get("altitude") or 0))) if has_z \
                else QgsGeometry.fromPointXY(xy)
            feats.append(QgsVectorLayerUtils.createFeature(lyr, geom, self._attrs(fields, vals, tag)))
        n, note = self._commit(lyr, feats, [])
        if n < 0:
            self._bar("Could not save to %s — see the log" % lyr.name(), _MSG_WARN)
        return n, note

    def _write_tracks(self, lyr, by_dev):
        fields = lyr.fields()
        names = fields.names()
        name_field = self._name_field(names, self.cfg["tracks_name_field"])
        w = self.win

        # "Keep most recent": earlier tracks of the same device, by device_id or name
        delete_ids = []
        if int(self.cfg["tracks_mode"]) == 1:
            match = "device_id" if "device_id" in names else name_field
            if not match:
                self._bar("Tracks layer has no device_id or name field — adding instead of replacing",
                          _MSG_WARN)
            else:
                keys = {str(d) if match == "device_id" else self._name(d) for d in by_dev}
                req = QgsFeatureRequest().setSubsetOfAttributes([match], fields)
                for f in lyr.getFeatures(req):
                    v = f[match]
                    if not _is_null(v) and str(v) in keys:
                        delete_ids.append(f.id())

        tr = QgsCoordinateTransform(WGS84, lyr.crs(), QgsProject.instance())
        saved = datetime.now(timezone.utc)
        feats = []
        for dev_id, pts in by_dev.items():
            xy = [tr.transform(QgsPointXY(p["longitude"], p["latitude"])) for p in pts]
            zs = [float(p.get("altitude") or 0) for p in pts]
            ms = [p["_t"].timestamp() for p in pts]
            if len(xy) == 1:             # a line needs two vertices
                xy, zs, ms = xy * 2, zs * 2, ms * 2
            line = QgsLineString([q.x() for q in xy], [q.y() for q in xy], zs, ms)
            vals = {
                "device_id": dev_id, "name": self._name(dev_id),
                "start_time": pts[0]["_t"], "last_update": pts[-1]["_t"],
                "start_local": _local_text(pts[0]["_t"]), "last_local": _local_text(pts[-1]["_t"]),
                "from_time": w["frm"], "to_time": w["to"], "n_points": len(pts), "saved_at": saved,
            }
            if name_field:
                vals[name_field] = vals["name"]
            feats.append(QgsVectorLayerUtils.createFeature(
                lyr, QgsGeometry(line), self._attrs(fields, vals, w["tag"])))
        n, note = self._commit(lyr, feats, delete_ids)
        if n < 0:
            self._bar("Could not save to %s — see the log" % lyr.name(), _MSG_WARN)
        return n, note
