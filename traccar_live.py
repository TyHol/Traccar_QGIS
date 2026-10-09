"""
Traccar Live – QGIS Plugin
Polls /api/devices and /api/positions (Basic Auth) on a configurable schedule.

On each fetch, three independent options can be active simultaneously:
  A  Keep live    – replace a point layer with current positions
  B  Add points   – append every position as a new point feature (history)
  C  Add to lines – append every position as a new vertex on the device's line track

B and C can share a single GeoPackage (two tables in one file).
Last-fetch status is shown at the top of the Settings dialog, not the message bar.

Compatible with QGIS 3.16+ (PyQt5) and QGIS 4.x (PyQt6).
"""

import json
import base64
import hashlib
import calendar
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as _quote

from qgis.PyQt.QtCore    import QTimer, QSettings, QDateTime, Qt
from qgis.PyQt.QtWidgets import (
    QAction, QApplication, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QDateTimeEdit, QComboBox,
    QDialogButtonBox, QPushButton, QTabWidget, QTextEdit,
    QGroupBox, QMessageBox, QCheckBox, QRadioButton, QWidget,
    QFileDialog, QFrame,
)
from qgis.gui  import QgsMapLayerComboBox
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsMapLayerProxyModel,
    QgsProject, QgsVectorLayer, QgsField, QgsFields,
    QgsFeature, QgsGeometry, QgsPointXY, QgsPoint,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter,
    QgsMessageLog, Qgis,
    QgsCategorizedSymbolRenderer, QgsRendererCategory,
    QgsMarkerSymbol, QgsLineSymbol, QgsWkbTypes, NULL,
)

# ── QGIS 3 / 4  (PyQt5 / PyQt6) compatibility ───────────────────────────────

try:
    from qgis.PyQt.QtCore import QVariant
    _INT, _STR, _DBL = QVariant.Int, QVariant.String, QVariant.Double
    _DT = QVariant.DateTime
except (ImportError, AttributeError):
    _INT, _STR, _DBL = 2, 10, 6
    _DT = 16   # QMetaType::QDateTime


def _e(root, *chain):
    """Resolve root.a.b (PyQt6) with fallback to root.b (PyQt5)."""
    try:
        obj = root
        for attr in chain:
            obj = getattr(obj, attr)
        return obj
    except AttributeError:
        return getattr(root, chain[-1])


_ECHO_PWD   = _e(QLineEdit,              "EchoMode",             "Password")
_BTN_OK     = _e(QDialogButtonBox,       "StandardButton",       "Ok")
_BTN_CANCEL = _e(QDialogButtonBox,       "StandardButton",       "Cancel")
_DLG_OK     = _e(QDialog,               "DialogCode",           "Accepted")
_MSG_WARN   = _e(Qgis,                  "MessageLevel",         "Warning")
_MSG_CRIT   = _e(Qgis,                  "MessageLevel",         "Critical")
_MSG_INFO   = _e(Qgis,                  "MessageLevel",         "Info")
_PT_LYR     = _e(QgsMapLayerProxyModel, "LayerType",            "PointLayer")
_LN_LYR     = _e(QgsMapLayerProxyModel, "LayerType",            "LineLayer")
_VFW_OK     = _e(QgsVectorFileWriter,   "WriterError",          "NoError")
_GPKG_NEW   = _e(QgsVectorFileWriter,   "ActionOnExistingFile", "CreateOrOverwriteFile")
_GPKG_LAYER = _e(QgsVectorFileWriter,   "ActionOnExistingFile", "CreateOrOverwriteLayer")

# ── Constants ────────────────────────────────────────────────────────────────

LIVE_LAYER_NAME = "Traccar – Live Positions"
SETTINGS_NS     = "TraccarLive"
MENU_LABEL      = "&Traccar Live"
DEFAULT_URL     = "https://server.traccar.org"

POINT_FIELDS = [
    QgsField("device_id",  _INT, "Device ID"),
    QgsField("name",       _STR, "Device Name"),
    QgsField("status",     _STR, "Status"),
    QgsField("speed_kmh",  _DBL, "Speed (km/h)"),
    QgsField("course",     _DBL, "Course (°)"),
    QgsField("altitude_m", _DBL, "Altitude (m)"),
    QgsField("fix_time",   _DT,  "Fix Time"),
    QgsField("battery",    _DBL, "Battery (%)"),
    QgsField("address",    _STR, "Address"),
    QgsField("motion",     _STR, "Motion"),
]
POINT_HISTORY_EXTRA = QgsField("fetched_at", _DT, "Fetched At (UTC)")

LINE_FIELDS = [
    QgsField("device_id",   _INT, "Device ID"),
    QgsField("name",        _STR, "Device Name"),
    QgsField("start_time",  _DT,  "Track Start"),
    QgsField("last_update", _DT,  "Last Update"),
]

# Optional field auto-filled by QField plugins (e.g. Traccar_QField) with a
# project-defined expression such as:
#   'KMRT-' || format_date(now(),'ddd-dd/MM/yy')||'-1'
# Added to new GeoPackage layers below, unless the schema already has it.
INCIDENT_REF_FIELD = QgsField("incident_ref", _STR, "Incident Ref")


def _fields_with_incident_ref(fields):
    """Return `fields` plus an `incident_ref` text field, unless already present."""
    if any(f.name() == "incident_ref" for f in fields):
        return list(fields)
    return list(fields) + [INCIDENT_REF_FIELD]

_PT_FIELD_MAP = {
    "device_id": "device_id", "name": "name", "status": "status",
    "speed_kmh": "speed_kmh", "course": "course", "altitude_m": "altitude",
    "fix_time":  "fix_time",  "battery": "battery", "address": "address",
    "motion":    "motion",
}

# ── Shared "quick range" timeframe options ──────────────────────────────────
# Used both by the Fetch Logs quick-range combo and the cull-by-age combo.
# (label, minutes) — minutes == 0 means "— custom date range —".
TIMEFRAME_OPTIONS = [
    ("— custom date range —", 0),
    ("Last 15 minutes",       15),
    ("Last 30 minutes",       30),
    ("Last 1 hour",           60),
    ("Last 2 hours",          120),
    ("Last 3 hours",          180),
    ("Last 6 hours",          360),
    ("Last 12 hours",         720),
    ("Last 18 hours",         1080),
    ("Last 1 day",            1440),
    ("Last 3 days",           4320),
    ("Last 1 week",           10080),
    ("Last 2 weeks",          20160),
    ("Last 1 month",          43200),
    ("Last 3 months",         129600),
]


# ── M-value helper ───────────────────────────────────────────────────────────

def _to_qdt(s: str):
    """Convert a Traccar fixTime ISO string to a UTC QDateTime.
    e.g. '2026-06-16T12:34:56.000+0000' → QDateTime(2026,6,16,12,34,56, UTC)
    Returns None if the string is empty or unparseable."""
    if not s:
        return None
    try:
        dt = QDateTime.fromString(s[:19], "yyyy-MM-ddTHH:mm:ss")
        if not dt.isValid():
            return None
        try:
            dt.setTimeSpec(Qt.UTC)
        except AttributeError:
            dt.setTimeSpec(Qt.TimeSpec.UTC)   # Qt6
        return dt
    except Exception:
        return None


def _epoch_from_fix_time(fix_time_str: str) -> float:
    """Convert Traccar fixTime ISO string to Unix epoch seconds (UTC).
    Strips milliseconds and timezone suffix, then uses calendar.timegm so
    the bare datetime is treated as UTC (not local time).
    e.g. '2026-06-16T12:34:56.000+0000' → 1750074896.0
    Returns 0.0 if the string is empty or unparseable."""
    if not fix_time_str:
        return 0.0
    try:
        s = fix_time_str[:19]   # "YYYY-MM-DDTHH:MM:SS" — drop ms and tz
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return float(calendar.timegm(dt.timetuple()))
    except Exception:
        return 0.0


# ── Categorized-renderer auto-update ─────────────────────────────────────────

def _device_color(name: str) -> QColor:
    """Deterministic hue from device name — same name always gets same colour."""
    hue = int(hashlib.md5(name.encode()).hexdigest()[:4], 16) % 360
    return QColor.fromHsv(hue, 180, 210)


def _sync_categorized_renderer(lyr, new_names: set) -> bool:
    """
    Rebuild the categorized renderer on `lyr` (categorized by the `name` field).

    - Reads ALL device names currently stored in the layer features.
    - Adds any names from `new_names` that aren't in the layer yet
      (i.e. the batch being written right now).
    - Preserves the colour of any device already in the renderer so
      that user-customised colours survive a refresh.
    - Assigns a deterministic colour (from device-name hash) for new devices.
    - Works whether or not the layer already had a categorized renderer.

    Returns True if the renderer was (re)applied.
    """
    # ── Collect all device names in the layer ─────────────────────────────
    all_names = set(new_names)
    for feat in lyr.getFeatures():
        val = feat["name"]
        if val is not None and val != NULL and str(val).strip():
            all_names.add(str(val))

    if not all_names:
        return False

    # ── Preserve colours already assigned by the user / previous sync ─────
    old_colors: dict = {}
    old_renderer = lyr.renderer()
    if isinstance(old_renderer, QgsCategorizedSymbolRenderer):
        for cat in old_renderer.categories():
            v = cat.value()
            if v and v != NULL:
                old_colors[str(v)] = cat.symbol().color()

    # ── Build fresh category list ─────────────────────────────────────────
    is_point   = lyr.geometryType() == QgsWkbTypes.PointGeometry
    categories = []
    for name in sorted(all_names):
        color = old_colors.get(name, _device_color(name))
        if is_point:
            sym = QgsMarkerSymbol.createSimple({"color": color.name()})
        else:
            sym = QgsLineSymbol.createSimple({"color":      color.name(),
                                              "line_width": "0.6"})
        categories.append(QgsRendererCategory(name, sym, name, True))

    lyr.setRenderer(QgsCategorizedSymbolRenderer("name", categories))
    lyr.triggerRepaint()
    return True


# ── HTTP helper ──────────────────────────────────────────────────────────────

def _api_get(base_url, username, password, path):
    url   = base_url.rstrip("/") + path
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    req   = urllib.request.Request(
        url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── GeoPackage helpers ───────────────────────────────────────────────────────

def _gpkg_write_layer(path, geom_uri, fields, layer_slug, action):
    """
    Write an empty schema layer to a GeoPackage.
    action = _GPKG_NEW   → create / overwrite whole file
    action = _GPKG_LAYER → add / overwrite just this table in an existing file
    Returns QgsVectorLayer or None.
    """
    flds = QgsFields()
    for f in fields:
        flds.append(QgsField(f.name(), f.type()))

    tmp = QgsVectorLayer(geom_uri, "tmp", "memory")
    tmp.dataProvider().addAttributes(flds)
    tmp.updateFields()

    opts                    = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName         = "GPKG"
    opts.layerName          = layer_slug
    opts.fileEncoding       = "UTF-8"
    opts.actionOnExistingFile = action

    err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
        tmp, path, QgsProject.instance().transformContext(), opts)
    if err != _VFW_OK:
        return None, msg

    lyr = QgsVectorLayer(f"{path}|layername={layer_slug}", layer_slug, "ogr")
    return (lyr if lyr.isValid() else None), ""


def _hr():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


# ── Settings dialog ──────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent,
                 server_url, username, password, interval_min,
                 live_layer_id,
                 append_pts,  pt_layer_id,
                 append_lines, ln_layer_id,
                 fetch_history=False,
                 cull_by_count=False, cull_max_per_device=500,
                 cull_by_age=False, cull_age_minutes=1440,
                 last_fetch=""):
        super().__init__(parent)
        self.setWindowTitle("Traccar Live – Settings")
        self.setMinimumWidth(490)
        layout = QVBoxLayout(self)

        # ── Last-fetch status (top of dialog) ────────────────────────────────
        if last_fetch:
            status_box = QLabel(f"Last fetched:  {last_fetch}")
            status_box.setStyleSheet(
                "background:#E3F2FD; color:#1565C0; padding:6px 10px;"
                "border-radius:4px; font-style:italic;")
            layout.addWidget(status_box)

        # ── Connection ───────────────────────────────────────────────────────
        conn = QGroupBox("Connection")
        form = QFormLayout(conn)

        self.url_edit = QLineEdit(server_url)
        self.url_edit.setPlaceholderText("https://server.traccar.org")
        form.addRow("Server URL:", self.url_edit)

        self.user_edit = QLineEdit(username)
        self.user_edit.setPlaceholderText("you@example.com")
        form.addRow("Username / Email:", self.user_edit)

        self.pass_edit = QLineEdit(password)
        self.pass_edit.setEchoMode(_ECHO_PWD)
        form.addRow("Password:", self.pass_edit)

        layout.addWidget(conn)

        # ── Refresh ──────────────────────────────────────────────────────────
        ref      = QGroupBox("Auto-Refresh")
        ref_form = QFormLayout(ref)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setValue(interval_min)
        self.interval_spin.setSuffix(" min")
        ref_form.addRow("Interval:", self.interval_spin)

        self.history_chk = QCheckBox(
            "Fetch full track history between refreshes")
        self.history_chk.setChecked(fetch_history)
        self.history_chk.setToolTip(
            "When enabled, each fetch pulls every GPS fix recorded since the\n"
            "previous fetch using /api/positions?deviceId=X&from=…&to=…\n"
            "All intermediate points are added to the point layer (B) and as\n"
            "vertices to the track line (C).  One API call per device per fetch.\n"
            "Disable to fetch only the current position per device.")
        ref_form.addRow("", self.history_chk)
        layout.addWidget(ref)

        # ── On each fetch ─────────────────────────────────────────────────────
        fetch        = QGroupBox("On Each Fetch")
        fetch_layout = QVBoxLayout(fetch)

        # A ───────────────────────────────────────────────────────────────────
        self.keep_live_chk = QCheckBox(
            "A  Keep live layer updated  (latest fix per device, replaced every fetch)")
        self.keep_live_chk.setChecked(live_layer_id != "")
        fetch_layout.addWidget(self.keep_live_chk)

        # Sub-widget: radio buttons + layer combo, indented under the checkbox
        a_sub = QWidget()
        a_sub.setEnabled(live_layer_id != "")
        a_sub_layout = QVBoxLayout(a_sub)
        a_sub_layout.setContentsMargins(24, 0, 0, 0)
        a_sub_layout.setSpacing(4)

        self.a_temp_radio  = QRadioButton(
            "Temporary in-memory layer  (not saved when QGIS closes)")
        self.a_layer_radio = QRadioButton("Use existing layer:")
        a_layer_row        = QHBoxLayout()
        self.live_combo    = QgsMapLayerComboBox()
        self.live_combo.setFilters(_PT_LYR)
        self.live_combo.setAdditionalItems(["— none  (A disabled) —"])
        self.live_combo.setShowCrs(True)
        a_layer_row.addWidget(self.a_layer_radio)
        a_layer_row.addWidget(self.live_combo, 1)

        a_sub_layout.addWidget(self.a_temp_radio)
        a_sub_layout.addLayout(a_layer_row)
        fetch_layout.addWidget(a_sub)

        # Initialise radio state
        if live_layer_id == "<<temp>>" or live_layer_id == "":
            self.a_temp_radio.setChecked(True)
            self.live_combo.setEnabled(False)
        else:
            self.a_layer_radio.setChecked(True)
            lyr_a = QgsProject.instance().mapLayer(live_layer_id)
            if lyr_a:
                self.live_combo.setLayer(lyr_a)

        # Wire signals
        self.keep_live_chk.toggled.connect(a_sub.setEnabled)
        self.a_layer_radio.toggled.connect(self.live_combo.setEnabled)

        fetch_layout.addWidget(_hr())

        # B ───────────────────────────────────────────────────────────────────
        self.pts_chk = QCheckBox(
            "B  Append to point layer  (cumulative position history)")
        self.pts_chk.setChecked(append_pts)
        fetch_layout.addWidget(self.pts_chk)

        pts_row = QHBoxLayout()
        self.pts_combo = QgsMapLayerComboBox()
        self.pts_combo.setFilters(_PT_LYR)
        self.pts_combo.setAdditionalItems(["— none  (B disabled) —"])
        self.pts_combo.setShowCrs(True)
        if pt_layer_id:
            lyr = QgsProject.instance().mapLayer(pt_layer_id)
            if lyr:
                self.pts_combo.setLayer(lyr)
        pts_row.addWidget(self.pts_combo, 1)
        fetch_layout.addLayout(pts_row)

        fetch_layout.addWidget(_hr())

        # C ───────────────────────────────────────────────────────────────────
        self.lns_chk = QCheckBox(
            "C  Append vertices to line layer  (one track-line per device)")
        self.lns_chk.setChecked(append_lines)
        fetch_layout.addWidget(self.lns_chk)

        lns_row = QHBoxLayout()
        self.lns_combo = QgsMapLayerComboBox()
        self.lns_combo.setFilters(_LN_LYR)
        self.lns_combo.setAdditionalItems(["— none  (C disabled) —"])
        self.lns_combo.setShowCrs(True)
        if ln_layer_id:
            lyr = QgsProject.instance().mapLayer(ln_layer_id)
            if lyr:
                self.lns_combo.setLayer(lyr)
        lns_row.addWidget(self.lns_combo, 1)
        fetch_layout.addLayout(lns_row)

        fetch_layout.addWidget(_hr())

        # Single combined GeoPackage button ───────────────────────────────────
        gpkg_row = QHBoxLayout()
        gpkg_lbl = QLabel("A + B + C:")
        gpkg_lbl.setFixedWidth(70)
        gpkg_row.addWidget(gpkg_lbl)
        new_gpkg_btn = QPushButton("New GeoPackage…  (creates all three layers in one file)")
        new_gpkg_btn.clicked.connect(self._new_combined_gpkg)
        gpkg_row.addWidget(new_gpkg_btn, 1)
        fetch_layout.addLayout(gpkg_row)

        layout.addWidget(fetch)

        # ── Layer B housekeeping (culling) ──────────────────────────────────────
        cull        = QGroupBox("Layer B Housekeeping  (point history)")
        cull_layout = QVBoxLayout(cull)

        # Cull by count (per device)
        count_row = QHBoxLayout()
        self.cull_count_chk = QCheckBox("Cull by count — keep at most")
        self.cull_count_chk.setChecked(cull_by_count)
        count_row.addWidget(self.cull_count_chk)
        self.cull_max_spin = QSpinBox()
        self.cull_max_spin.setRange(1, 100000)
        self.cull_max_spin.setSingleStep(50)
        self.cull_max_spin.setValue(cull_max_per_device)
        self.cull_max_spin.setEnabled(cull_by_count)
        count_row.addWidget(self.cull_max_spin)
        count_row.addWidget(QLabel("point(s) per device"))
        count_row.addStretch()
        cull_layout.addLayout(count_row)
        self.cull_count_chk.toggled.connect(self.cull_max_spin.setEnabled)

        # Cull by age
        age_row = QHBoxLayout()
        self.cull_age_chk = QCheckBox("Cull by age — remove points older than")
        self.cull_age_chk.setChecked(cull_by_age)
        age_row.addWidget(self.cull_age_chk)
        self.cull_age_combo = QComboBox()
        for label, _minutes in TIMEFRAME_OPTIONS[1:]:   # skip "— custom date range —"
            self.cull_age_combo.addItem(label, _minutes)
        # Restore saved selection (default to first real entry if not found)
        idx = self.cull_age_combo.findData(cull_age_minutes)
        self.cull_age_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.cull_age_combo.setEnabled(cull_by_age)
        age_row.addWidget(self.cull_age_combo, 1)
        cull_layout.addLayout(age_row)
        self.cull_age_chk.toggled.connect(self.cull_age_combo.setEnabled)

        cull_note = QLabel(
            "Applies after each automatic Live fetch (not after manual Fetch Logs).\n"
            "Both options can be enabled together.")
        cull_note.setWordWrap(True)
        cull_note.setStyleSheet("color:#555; font-style:italic;")
        cull_layout.addWidget(cull_note)

        layout.addWidget(cull)

        # ── Test / OK / Cancel ────────────────────────────────────────────────
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test)
        layout.addWidget(test_btn)

        buttons = QDialogButtonBox(_BTN_OK | _BTN_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        # A: live layer ID
        if not self.keep_live_chk.isChecked():
            live_layer_id = ""
        elif self.a_temp_radio.isChecked():
            live_layer_id = "<<temp>>"
        else:
            live_lyr = self.live_combo.currentLayer()
            live_layer_id = live_lyr.id() if live_lyr else "<<temp>>"

        pt_lyr = self.pts_combo.currentLayer()
        ln_lyr = self.lns_combo.currentLayer()
        return (
            self.url_edit.text().rstrip("/"),
            self.user_edit.text().strip(),
            self.pass_edit.text(),
            self.interval_spin.value(),
            live_layer_id,
            self.pts_chk.isChecked(),
            pt_lyr.id() if pt_lyr else "",
            self.lns_chk.isChecked(),
            ln_lyr.id() if ln_lyr else "",
            self.history_chk.isChecked(),
            self.cull_count_chk.isChecked(),
            self.cull_max_spin.value(),
            self.cull_age_chk.isChecked(),
            self.cull_age_combo.currentData(),
        )

    def _test(self):
        url, user, pwd, *_ = self.values()
        try:
            devices = _api_get(url, user, pwd, "/api/devices")
            QMessageBox.information(self, "Connection OK",
                f"Connected — {len(devices)} device(s) visible to this account.")
        except urllib.error.HTTPError as exc:
            QMessageBox.warning(self, "HTTP Error", f"{exc.code} {exc.reason}")
        except Exception as exc:
            QMessageBox.warning(self, "Connection Failed", str(exc))

    def _new_combined_gpkg(self):
        """Create one GeoPackage with live (A), history (B) and track (C) layers."""
        path, _ = QFileDialog.getSaveFileName(
            self, "Create Traccar GeoPackage", "", "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"

        errors = []

        # A ── live layer (creates the file) ──────────────────────────────────
        live_lyr, err = _gpkg_write_layer(
            path, "Point?crs=EPSG:4326",
            _fields_with_incident_ref(POINT_FIELDS),
            "traccar_live", _GPKG_NEW)
        if live_lyr:
            live_lyr.setName("Traccar Live")
            QgsProject.instance().addMapLayer(live_lyr)
            self.live_combo.setLayer(live_lyr)
            self.a_layer_radio.setChecked(True)
            self.keep_live_chk.setChecked(True)
        else:
            errors.append(f"traccar_live: {err}")

        # B ── point history layer ─────────────────────────────────────────────
        pt_lyr, err = _gpkg_write_layer(
            path, "Point?crs=EPSG:4326",
            _fields_with_incident_ref(list(POINT_FIELDS) + [POINT_HISTORY_EXTRA]),
            "traccar_points", _GPKG_LAYER)
        if pt_lyr:
            pt_lyr.setName("Traccar Points")
            QgsProject.instance().addMapLayer(pt_lyr)
            self.pts_combo.setLayer(pt_lyr)
            self.pts_chk.setChecked(True)
        else:
            errors.append(f"traccar_points: {err}")

        # C ── track line layer ────────────────────────────────────────────────
        # LineStringZM: Z = altitude (m), M = Unix epoch seconds (UTC).
        # M enables QGIS temporal controller animation along the track.
        ln_lyr, err = _gpkg_write_layer(
            path, "LineStringZM?crs=EPSG:4326",
            _fields_with_incident_ref(LINE_FIELDS),
            "traccar_tracks", _GPKG_LAYER)
        if ln_lyr:
            ln_lyr.setName("Traccar Tracks")
            QgsProject.instance().addMapLayer(ln_lyr)
            self.lns_combo.setLayer(ln_lyr)
            self.lns_chk.setChecked(True)
        else:
            errors.append(f"traccar_tracks: {err}")

        if errors:
            QMessageBox.warning(self, "GeoPackage Error", "\n".join(errors))
        else:
            QMessageBox.information(self, "GeoPackage Created",
                f"Three layers created in one file:\n\n"
                f"  • traccar_live    (A — live positions, replaced each fetch)\n"
                f"  • traccar_points  (B — position history, appended each fetch)\n"
                f"  • traccar_tracks  (C — device track lines)\n\n{path}")


# ── Fetch-logs dialog ────────────────────────────────────────────────────────

class FetchLogsDialog(QDialog):
    """
    Pull historical GPS positions from a user-defined time window.
    Works for all devices (online or offline).
    Writes to the point layer (B) and/or track line layer (C) already
    configured in Settings, using the same pipeline as the live fetch.

    Tab 1 – Fetch:    time range, device selector, output-layer checkboxes.
    Tab 2 – Log:      read-only summary of every fetch made this session
                      (both auto-timer fetches and manual fetches from here).
    """

    def __init__(self, parent, plugin):
        super().__init__(parent)
        self.setWindowTitle("Traccar – Fetch Historical Logs")
        self.setMinimumWidth(520)
        self.setMinimumHeight(480)
        self._plugin  = plugin
        self._devices = []

        root = QVBoxLayout(self)

        # ── Tab widget ────────────────────────────────────────────────────
        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        # ── Tab 1: Fetch form ─────────────────────────────────────────────
        tab1        = QFrame()
        tab1_layout = QVBoxLayout(tab1)
        self._tabs.addTab(tab1, "Fetch")

        # Time range
        rng      = QGroupBox("Time range  (your local time)")
        rng_form = QFormLayout(rng)

        self.quick_range_combo = QComboBox()
        for label, minutes in TIMEFRAME_OPTIONS:
            self.quick_range_combo.addItem(label, minutes)
        self.quick_range_combo.currentIndexChanged.connect(self._on_quick_range_changed)
        rng_form.addRow("Quick range:", self.quick_range_combo)

        self.from_dt = QDateTimeEdit()
        self.from_dt.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self.from_dt.setCalendarPopup(True)
        self.from_dt.setDateTime(QDateTime.currentDateTime().addDays(-1))
        rng_form.addRow("From:", self.from_dt)

        self.to_dt = QDateTimeEdit()
        self.to_dt.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self.to_dt.setCalendarPopup(True)
        self.to_dt.setDateTime(QDateTime.currentDateTime())
        rng_form.addRow("To:", self.to_dt)

        tab1_layout.addWidget(rng)

        # Devices
        dev        = QGroupBox("Devices")
        dev_layout = QVBoxLayout(dev)

        self.all_chk = QCheckBox("All devices  (including offline)")
        self.all_chk.setChecked(True)
        dev_layout.addWidget(self.all_chk)

        self.dev_combo = QComboBox()
        self.dev_combo.setEnabled(False)
        self.all_chk.toggled.connect(
            lambda on: self.dev_combo.setEnabled(not on))
        dev_layout.addWidget(self.dev_combo)

        tab1_layout.addWidget(dev)

        # Output layers
        out        = QGroupBox("Write to")
        out_layout = QVBoxLayout(out)

        pt_lyr  = QgsProject.instance().mapLayer(plugin.pt_layer_id)
        ln_lyr  = QgsProject.instance().mapLayer(plugin.ln_layer_id)
        pt_name = f"  →  {pt_lyr.name()}" if pt_lyr else \
                  "  (not configured — set in Settings)"
        ln_name = f"  →  {ln_lyr.name()}" if ln_lyr else \
                  "  (not configured — set in Settings)"

        self.pts_chk = QCheckBox(f"B  Point layer{pt_name}")
        self.pts_chk.setChecked(bool(pt_lyr))
        self.pts_chk.setEnabled(bool(pt_lyr))
        out_layout.addWidget(self.pts_chk)

        self.lns_chk = QCheckBox(f"C  Track line layer{ln_name}")
        self.lns_chk.setChecked(bool(ln_lyr))
        self.lns_chk.setEnabled(bool(ln_lyr))
        out_layout.addWidget(self.lns_chk)

        tab1_layout.addWidget(out)

        # Point layer limit
        limit       = QGroupBox("Point Layer Limit")
        limit_row   = QHBoxLayout(limit)
        self.limit_chk = QCheckBox("Limit point layer (B) to the most recent")
        self.limit_chk.setChecked(plugin.fetch_limit_pts)
        limit_row.addWidget(self.limit_chk)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 100000)
        self.limit_spin.setSingleStep(50)
        self.limit_spin.setValue(plugin.fetch_max_points)
        self.limit_spin.setEnabled(plugin.fetch_limit_pts)
        limit_row.addWidget(self.limit_spin)
        limit_row.addWidget(QLabel("point(s) per device"))
        limit_row.addStretch()
        self.limit_chk.toggled.connect(self.limit_spin.setEnabled)
        tab1_layout.addWidget(limit)

        # Status label
        self.status_lbl = QLabel("Loading devices…")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("color:#555; font-style:italic;")
        tab1_layout.addWidget(self.status_lbl)

        tab1_layout.addStretch()

        # ── Tab 2: Fetch log ──────────────────────────────────────────────
        tab2        = QFrame()
        tab2_layout = QVBoxLayout(tab2)
        self._tabs.addTab(tab2, "Fetch Log")

        log_lbl = QLabel(
            "Summary of every fetch this session  "
            "(auto-timer + manual).  Most recent first.")
        log_lbl.setWordWrap(True)
        tab2_layout.addWidget(log_lbl)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log_view.setFontFamily("Courier New")
        tab2_layout.addWidget(self._log_view)

        # ── Buttons (shared, below tabs) ──────────────────────────────────
        btn_row        = QHBoxLayout()
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.clicked.connect(self._do_fetch)
        btn_row.addWidget(self.fetch_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

        # Switch Fetch button visibility by tab
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._refresh_log()
        self._load_devices()

    # ── Quick range ───────────────────────────────────────────────────────

    def _on_quick_range_changed(self, idx):
        """index 0 ('— custom date range —') leaves From/To editable;
        any other entry disables them — _do_fetch() computes the range."""
        custom = (idx == 0)
        self.from_dt.setEnabled(custom)
        self.to_dt.setEnabled(custom)

    # ── Tab change ────────────────────────────────────────────────────────

    def _on_tab_changed(self, idx):
        # Refresh log whenever user switches to tab 2
        if idx == 1:
            self._refresh_log()
        self.fetch_btn.setVisible(idx == 0)

    # ── Load device list ──────────────────────────────────────────────────

    def _load_devices(self):
        try:
            self._devices = _api_get(
                self._plugin.server_url, self._plugin.username,
                self._plugin.password, "/api/devices")
            self.dev_combo.clear()
            for d in self._devices:
                label = (f"{d.get('name', d['id'])}"
                         f"  [{d.get('status', '?')}]")
                self.dev_combo.addItem(label, d["id"])
            n = len(self._devices)
            self.status_lbl.setText(
                f"{n} device(s) found — set the time range and click Fetch.")
            self.status_lbl.setStyleSheet("")
            self.fetch_btn.setEnabled(True)
        except Exception as exc:
            self.status_lbl.setText(f"Could not load devices: {exc}")
            self.status_lbl.setStyleSheet("color:red;")

    # ── Fetch log refresh ─────────────────────────────────────────────────

    def _refresh_log(self):
        entries = self._plugin.fetch_log
        if not entries:
            self._log_view.setPlainText(
                "(No fetches yet this session — start Live or Fetch manually.)")
            return

        lines = []
        # Most recent first
        for e in reversed(entries):
            hist_tag = " [history]" if e.get("hist") else ""
            src_tag  = " [manual]"  if e.get("manual") else " [auto]"
            lines.append(
                f"── {e['ts']}{src_tag}{hist_tag}  "
                f"{e['n_devs']} device(s), "
                f"{e['n_online']} online, "
                f"{e['n_pts']} position(s)")
            for dr in e.get("devs", []):
                status_icon = "●" if dr["status"] == "online" else "○"
                lines.append(
                    f"   {status_icon} {dr['name']:<20} "
                    f"status={dr['status']:<8} "
                    f"pts={dr['pts']:<5} "
                    f"loc={dr['loc']}  "
                    f"fix={dr['fix']}")
            lines.append("")

        self._log_view.setPlainText("\n".join(lines))

    # ── Fetch ─────────────────────────────────────────────────────────────

    def _do_fetch(self):
        # Quick range (last X minutes/hours) takes precedence over the date fields
        qr_minutes = self.quick_range_combo.currentData()
        if qr_minutes:
            now_utc  = QDateTime.currentDateTimeUtc()
            from_utc = now_utc.addSecs(-qr_minutes * 60)
            to_utc   = now_utc
        else:
            # Validate time range
            from_utc = self.from_dt.dateTime().toUTC()
            to_utc   = self.to_dt.dateTime().toUTC()
            if from_utc >= to_utc:
                self.status_lbl.setText("'From' must be earlier than 'To'.")
                return

        # Traccar expects ISO 8601 UTC
        from_iso = from_utc.toString("yyyy-MM-ddTHH:mm:ss") + "Z"
        to_iso   = to_utc.toString("yyyy-MM-ddTHH:mm:ss")   + "Z"

        if not self.pts_chk.isChecked() and not self.lns_chk.isChecked():
            self.status_lbl.setText("Select at least one output layer.")
            return

        dev_ids = ([d["id"] for d in self._devices] if self.all_chk.isChecked()
                   else [self.dev_combo.currentData()])
        if not dev_ids:
            self.status_lbl.setText("No devices available.")
            return

        self.fetch_btn.setEnabled(False)
        device_info = {d["id"]: {"name":   d.get("name",   str(d["id"])),
                                  "status": d.get("status", "")}
                       for d in self._devices}

        from_enc = _quote(from_iso)
        to_enc   = _quote(to_iso)
        all_proc: list = []
        errors:   list = []
        pts_by_dev: dict = {}

        for i, dev_id in enumerate(dev_ids):
            name = device_info[dev_id]["name"]
            self.status_lbl.setText(
                f"Fetching {i + 1}/{len(dev_ids)}: {name}…")
            QApplication.processEvents()   # keep UI responsive between calls

            path = (f"/api/positions?deviceId={dev_id}"
                    f"&from={from_enc}&to={to_enc}")
            try:
                raw  = _api_get(self._plugin.server_url,
                                self._plugin.username,
                                self._plugin.password, path)
                info = device_info[dev_id]
                batch = []
                for pos in raw:
                    lat = pos.get("latitude")
                    lon = pos.get("longitude")
                    if lat is None or lon is None:
                        continue
                    attrs = pos.get("attributes", {})
                    rec = {
                        "lon":       lon,      "lat":      lat,
                        "device_id": dev_id,   "name":     info["name"],
                        "status":    info["status"],
                        "speed_kmh": round((pos.get("speed") or 0.0) * 1.852, 1),
                        "course":    pos.get("course")   or 0.0,
                        "altitude":  pos.get("altitude") or 0.0,
                        "fix_time":  pos.get("fixTime",  ""),
                        "battery":   attrs.get("batteryLevel"),
                        "address":   pos.get("address")  or "",
                        "motion":    str(attrs.get("motion", "")),
                    }
                    batch.append(rec)
                all_proc.extend(batch)
                pts_by_dev[dev_id] = batch
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                pts_by_dev[dev_id] = []

        if not all_proc:
            msg = "No positions found in this time range."
            if errors:
                msg += "\n\nErrors:\n" + "\n".join(errors)
            self.status_lbl.setText(msg)
            self.status_lbl.setStyleSheet("color:orange;")
            self.fetch_btn.setEnabled(True)
            return

        # Persist the Point Layer Limit controls for next time
        self._plugin.fetch_limit_pts  = self.limit_chk.isChecked()
        self._plugin.fetch_max_points = self.limit_spin.value()
        self._plugin._save_settings()

        # Write via the plugin's existing layer pipelines
        written = []
        if self.pts_chk.isChecked():
            pts_for_b = all_proc
            if self.limit_chk.isChecked():
                max_pts = self.limit_spin.value()
                by_dev: dict = {}
                for p in all_proc:
                    by_dev.setdefault(p["device_id"], []).append(p)
                pts_for_b = []
                for arr in by_dev.values():
                    arr.sort(key=lambda x: x["fix_time"], reverse=True)
                    pts_for_b.extend(arr[:max_pts])
            self._plugin._append_to_point_layer(pts_for_b)
            extra = f" of {len(all_proc)}" if len(pts_for_b) != len(all_proc) else ""
            written.append(f"{len(pts_for_b)} point(s){extra}")
        if self.lns_chk.isChecked():
            self._plugin._append_to_line_layer(all_proc)
            n_devs = len({p["device_id"] for p in all_proc})
            written.append(f"{n_devs} track(s) updated")

        msg = "✓  Done — " + ",  ".join(written) + "."
        if errors:
            msg += f"\n⚠  {len(errors)} device(s) had errors: " + \
                   ";  ".join(errors)
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color:green;" if not errors
                                      else "color:orange;")
        self.fetch_btn.setEnabled(True)

        # ── Add entry to the session log ──────────────────────────────────
        ts      = datetime.now().strftime("%H:%M:%S")
        dev_rows = []
        for dev_id, info in device_info.items():
            pts  = pts_by_dev.get(dev_id, [])
            last = pts[-1] if pts else None
            dev_rows.append({
                "name":   info["name"],
                "status": info["status"],
                "pts":    len(pts),
                "loc":    (f"{last['lat']:.5f}, {last['lon']:.5f}"
                           if last else "—"),
                "fix":    (last["fix_time"][:19].replace("T", " ")
                           if last and last.get("fix_time") else "—"),
            })
        self._plugin.fetch_log.append({
            "ts":       ts,
            "manual":   True,
            "hist":     False,
            "n_devs":   len(device_info),
            "n_online": sum(1 for v in device_info.values()
                            if v["status"] == "online"),
            "n_pts":    len(all_proc),
            "devs":     dev_rows,
        })
        self._refresh_log()


# ── Main plugin class ─────────────────────────────────────────────────────────

class TraccarLive:

    def __init__(self, iface):
        self.iface           = iface
        self.timer           = QTimer()
        self.timer.timeout.connect(self.fetch_and_update)
        self._actions        = []
        self.last_fetch_info = ""      # shown at top of settings dialog
        self.fetch_log: list = []     # entries shown on Fetch Log tab
        self._load_settings()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def initGui(self):
        def _add(label, slot, checkable=False, tip=""):
            act = QAction(label, self.iface.mainWindow())
            act.setToolTip(tip)
            if checkable:
                act.setCheckable(True)
            act.triggered.connect(slot)
            self.iface.addPluginToMenu(MENU_LABEL, act)
            self.iface.addToolBarIcon(act)
            self._actions.append(act)
            return act

        _add("⚙ Traccar Settings…", self.open_settings,
             tip="Configure server, credentials and layer options")
        self._act_live = _add("▶ Traccar Live", self._toggle_live, checkable=True,
             tip="Start / stop automatic position updates")
        _add("↻ Fetch Now", self.fetch_and_update,
             tip="Fetch positions immediately (one-off)")
        _add("📅 Fetch Logs…", self.open_fetch_logs,
             tip="Pull historical GPS positions for any time range / device")

    def unload(self):
        self.timer.stop()
        for act in self._actions:
            self.iface.removePluginMenu(MENU_LABEL, act)
            self.iface.removeToolBarIcon(act)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self):
        s = QSettings()
        self.server_url     = s.value(f"{SETTINGS_NS}/server_url",     DEFAULT_URL)
        self.username       = s.value(f"{SETTINGS_NS}/username",        "")
        self.password       = s.value(f"{SETTINGS_NS}/password",        "")
        self.interval_min   = int(s.value(f"{SETTINGS_NS}/interval_min", 3))
        # live_layer_id: "" = off, "<<temp>>" = auto memory layer, else real layer id
        # Backward-compat: if old keep_live was True and no live_layer_id saved, use temp
        _old_keep = s.value(f"{SETTINGS_NS}/keep_live", None)
        self.live_layer_id  = s.value(f"{SETTINGS_NS}/live_layer_id",
                                      "<<temp>>" if _old_keep == "true" or _old_keep is True
                                      else "")
        self.append_pts     = s.value(f"{SETTINGS_NS}/append_pts",     False, type=bool)
        self.pt_layer_id    = s.value(f"{SETTINGS_NS}/pt_layer_id",    "")
        self.append_lines   = s.value(f"{SETTINGS_NS}/append_lines",   False, type=bool)
        self.ln_layer_id    = s.value(f"{SETTINGS_NS}/ln_layer_id",    "")
        self.fetch_history  = s.value(f"{SETTINGS_NS}/fetch_history",  False, type=bool)
        self.last_fetch_iso = s.value(f"{SETTINGS_NS}/last_fetch_iso", "")   # internal

        # ── Layer B housekeeping (culling) ───────────────────────────────────
        self.cull_by_count       = s.value(f"{SETTINGS_NS}/cull_by_count",       False, type=bool)
        self.cull_max_per_device = int(s.value(f"{SETTINGS_NS}/cull_max_per_device", 500))
        self.cull_by_age         = s.value(f"{SETTINGS_NS}/cull_by_age",         False, type=bool)
        self.cull_age_minutes    = int(s.value(f"{SETTINGS_NS}/cull_age_minutes", 1440))

        # ── Fetch Logs: per-device point-count limit ─────────────────────────
        self.fetch_limit_pts  = s.value(f"{SETTINGS_NS}/fetch_limit_pts",  True, type=bool)
        self.fetch_max_points = int(s.value(f"{SETTINGS_NS}/fetch_max_points", 150))

    def _save_settings(self):
        s = QSettings()
        s.setValue(f"{SETTINGS_NS}/server_url",     self.server_url)
        s.setValue(f"{SETTINGS_NS}/username",       self.username)
        s.setValue(f"{SETTINGS_NS}/password",       self.password)
        s.setValue(f"{SETTINGS_NS}/interval_min",   self.interval_min)
        s.setValue(f"{SETTINGS_NS}/live_layer_id",  self.live_layer_id)
        s.setValue(f"{SETTINGS_NS}/append_pts",     self.append_pts)
        s.setValue(f"{SETTINGS_NS}/pt_layer_id",    self.pt_layer_id)
        s.setValue(f"{SETTINGS_NS}/append_lines",   self.append_lines)
        s.setValue(f"{SETTINGS_NS}/ln_layer_id",    self.ln_layer_id)
        s.setValue(f"{SETTINGS_NS}/fetch_history",  self.fetch_history)
        s.setValue(f"{SETTINGS_NS}/last_fetch_iso", self.last_fetch_iso)

        s.setValue(f"{SETTINGS_NS}/cull_by_count",       self.cull_by_count)
        s.setValue(f"{SETTINGS_NS}/cull_max_per_device", self.cull_max_per_device)
        s.setValue(f"{SETTINGS_NS}/cull_by_age",         self.cull_by_age)
        s.setValue(f"{SETTINGS_NS}/cull_age_minutes",    self.cull_age_minutes)

        s.setValue(f"{SETTINGS_NS}/fetch_limit_pts",  self.fetch_limit_pts)
        s.setValue(f"{SETTINGS_NS}/fetch_max_points", self.fetch_max_points)

    def open_settings(self):
        dlg = SettingsDialog(
            self.iface.mainWindow(),
            self.server_url, self.username, self.password, self.interval_min,
            self.live_layer_id,
            self.append_pts,  self.pt_layer_id,
            self.append_lines, self.ln_layer_id,
            self.fetch_history,
            self.cull_by_count, self.cull_max_per_device,
            self.cull_by_age, self.cull_age_minutes,
            last_fetch=self.last_fetch_info,
        )
        if dlg.exec() == _DLG_OK:
            (self.server_url, self.username, self.password, self.interval_min,
             self.live_layer_id,
             self.append_pts,  self.pt_layer_id,
             self.append_lines, self.ln_layer_id,
             self.fetch_history,
             self.cull_by_count, self.cull_max_per_device,
             self.cull_by_age, self.cull_age_minutes) = dlg.values()
            self._save_settings()
            if self.timer.isActive():
                self.timer.start(self.interval_min * 60_000)

    def open_fetch_logs(self):
        dlg = FetchLogsDialog(self.iface.mainWindow(), self)
        dlg.exec()

    # ── Live toggle ───────────────────────────────────────────────────────────

    def _toggle_live(self, checked):
        if checked:
            self.fetch_and_update()
            self.timer.start(self.interval_min * 60_000)
            self._act_live.setText("⏹ Traccar Live (on)")
        else:
            self.timer.stop()
            self._act_live.setText("▶ Traccar Live")

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def fetch_and_update(self):
        if not self.username:
            self._warn("No credentials set — open Traccar Settings first.")
            return

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            devices_raw = _api_get(self.server_url, self.username,
                                   self.password, "/api/devices")
        except urllib.error.HTTPError as exc:
            self._warn(f"HTTP {exc.code}: {exc.reason}"); return
        except Exception as exc:
            self._warn(str(exc)); return

        device_info = {
            d["id"]: {"name": d.get("name", ""), "status": d.get("status", "")}
            for d in devices_raw
        }

        # ── Always fetch last-known positions for the live layer (A) ─────────
        # /api/positions (no range) returns Traccar's stored last-known fix for
        # every device — online OR offline — so layer A always shows all devices.
        try:
            live_raw = _api_get(self.server_url, self.username,
                                self.password, "/api/positions")
        except urllib.error.HTTPError as exc:
            self._warn(f"HTTP {exc.code}: {exc.reason}"); return
        except Exception as exc:
            self._warn(str(exc)); return

        # ── Collect positions for B + C (history range or reuse live_raw) ─────
        if self.fetch_history and self.last_fetch_iso:
            # Pull every fix per device since the last fetch
            positions_raw = []
            from_enc = _quote(self.last_fetch_iso)
            to_enc   = _quote(now_iso)
            errors   = []
            for dev_id in device_info:
                path = (f"/api/positions?deviceId={dev_id}"
                        f"&from={from_enc}&to={to_enc}")
                try:
                    positions_raw.extend(
                        _api_get(self.server_url, self.username, self.password, path))
                except Exception as exc:
                    errors.append(str(exc))
            if errors and not positions_raw:
                self._warn("History fetch failed: " + errors[0]); return
            if not positions_raw:
                positions_raw = live_raw   # fallback if nothing in range
        else:
            positions_raw = live_raw       # reuse — no second API call needed

        # ── Helper: parse a raw Traccar position dict ─────────────────────────
        def _parse(pos):
            lat = pos.get("latitude"); lon = pos.get("longitude")
            if lat is None or lon is None:
                return None
            dev_id = pos.get("deviceId", -1)
            info   = device_info.get(dev_id, {})
            attrs  = pos.get("attributes", {})
            return {
                "lon":       lon,        "lat":      lat,
                "device_id": dev_id,     "name":     info.get("name", str(dev_id)),
                "status":    info.get("status", ""),
                "speed_kmh": round((pos.get("speed") or 0.0) * 1.852, 1),
                "course":    pos.get("course")   or 0.0,
                "altitude":  pos.get("altitude") or 0.0,
                "fix_time":  pos.get("fixTime",  ""),
                "battery":   attrs.get("batteryLevel"),
                "address":   pos.get("address")  or "",
                "motion":    str(attrs.get("motion", "")),
            }

        # live_processed: one entry per device (last-known, incl. offline)
        live_processed = [r for r in (_parse(p) for p in live_raw) if r]
        # processed: all positions for B + C
        processed      = [r for r in (_parse(p) for p in positions_raw) if r]

        # Live layer (A) — fed from live_processed, so offline devices always shown
        if self.live_layer_id:
            self._update_live_layer(live_processed)

        if self.append_pts and self.pt_layer_id and processed:
            self._append_to_point_layer(processed)
            if self.cull_by_count or self.cull_by_age:
                self._cull_point_layer()

        if self.append_lines and self.ln_layer_id and processed:
            self._append_to_line_layer(processed)

        # Save timestamp for next history fetch
        self.last_fetch_iso = now_iso
        self._save_settings()

        # Status shown at top of Settings dialog
        active = (["A"] if self.live_layer_id  else []) + \
                 (["B"] if self.append_pts    else []) + \
                 (["C"] if self.append_lines  else [])
        mode   = "  [" + " + ".join(active) + "]" if active else ""
        hist   = "  (history)" if self.fetch_history else ""
        n_devs = len({p["device_id"] for p in processed})
        n_pts  = len(processed)
        ts     = datetime.now().strftime("%H:%M:%S")
        extra  = f"  ({n_pts} pts)" if n_pts > n_devs else ""
        self.last_fetch_info = f"{ts}  —  {n_devs} device(s){extra}{hist}{mode}"

        # Build a fetch-log entry (visible on Tab 2 of Fetch Logs dialog)
        pts_by_dev: dict = {}
        for p in processed:
            pts_by_dev.setdefault(p["device_id"], []).append(p)
        dev_rows = []
        for dev_id, info in device_info.items():
            pts   = pts_by_dev.get(dev_id, [])
            last  = pts[-1] if pts else None
            loc   = (f"{last['lat']:.5f}, {last['lon']:.5f}"
                     if last else "—")
            fix   = (last["fix_time"][:19].replace("T", " ")
                     if last and last.get("fix_time") else "—")
            dev_rows.append({
                "name":   info["name"],
                "status": info["status"],
                "pts":    len(pts),
                "loc":    loc,
                "fix":    fix,
            })
        self.fetch_log.append({
            "ts":       ts,
            "hist":     bool(self.fetch_history),
            "n_devs":   len(device_info),
            "n_online": sum(1 for v in device_info.values()
                            if v["status"] == "online"),
            "n_pts":    n_pts,
            "devs":     dev_rows,
        })

        QgsMessageLog.logMessage(
            f"[{ts}] {n_pts} position(s) for {n_devs} device(s){hist}{mode}",
            "Traccar Live", _MSG_INFO)

    # ── A: live layer ─────────────────────────────────────────────────────────

    def _get_or_create_temp_live_layer(self):
        """Return (or auto-create) the legacy in-memory live layer."""
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name() == LIVE_LAYER_NAME and isinstance(lyr, QgsVectorLayer):
                return lyr
        lyr = QgsVectorLayer("Point?crs=EPSG:4326", LIVE_LAYER_NAME, "memory")
        dp  = lyr.dataProvider()
        dp.addAttributes(POINT_FIELDS)
        lyr.updateFields()
        lyr.setCustomProperty("labeling",           "pal")
        lyr.setCustomProperty("labeling/enabled",   True)
        lyr.setCustomProperty("labeling/fieldName", "name")
        lyr.setCustomProperty("labeling/placement", "2")
        QgsProject.instance().addMapLayer(lyr)
        return lyr

    def _update_live_layer(self, processed):
        if self.live_layer_id == "<<temp>>":
            lyr = self._get_or_create_temp_live_layer()
        else:
            lyr = QgsProject.instance().mapLayer(self.live_layer_id)
            if not lyr or not lyr.isValid():
                self._warn("Live layer (A) not found in project — check Settings.")
                return

        dp = lyr.dataProvider()
        dp.truncate()
        feats = []
        for p in processed:
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])))
            for field in lyr.fields():
                fn = field.name()
                if fn == "fetched_at":
                    f[fn] = QDateTime.currentDateTimeUtc()
                elif fn == "fix_time" and "fix_time" in p:
                    f[fn] = _to_qdt(p["fix_time"])
                elif fn in _PT_FIELD_MAP and _PT_FIELD_MAP[fn] in p:
                    f[fn] = p[_PT_FIELD_MAP[fn]]
            feats.append(f)
        dp.addFeatures(feats)
        lyr.triggerRepaint()
        lyr.updateExtents()
        _sync_categorized_renderer(lyr, {p["name"] for p in processed})

    # ── B: point history ──────────────────────────────────────────────────────

    def _append_to_point_layer(self, processed):
        lyr = QgsProject.instance().mapLayer(self.pt_layer_id)
        if not lyr or not lyr.isValid():
            self._warn("Point history layer not found — check Settings.")
            return
        ts    = QDateTime.currentDateTimeUtc()
        feats = []
        for p in processed:
            f = QgsFeature(lyr.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])))
            for field in lyr.fields():
                fn = field.name()
                if fn == "fetched_at":
                    f[fn] = ts
                elif fn == "fix_time" and "fix_time" in p:
                    f[fn] = _to_qdt(p["fix_time"])
                elif fn in _PT_FIELD_MAP and _PT_FIELD_MAP[fn] in p:
                    f[fn] = p[_PT_FIELD_MAP[fn]]
            feats.append(f)
        ok, _ = lyr.dataProvider().addFeatures(feats)
        if ok:
            lyr.triggerRepaint()
            _sync_categorized_renderer(lyr, {p["name"] for p in processed})
        else:
            self._warn("Failed to write to point history layer.")

    def _cull_point_layer(self):
        """
        Remove old points from the point history layer (B), per the
        Layer B Housekeeping settings (cull_by_count / cull_by_age).
        Both can be active at once. Only called after periodic Live
        fetches — never from the Fetch Logs dialog.
        """
        if not (self.cull_by_count or self.cull_by_age):
            return
        lyr = QgsProject.instance().mapLayer(self.pt_layer_id)
        if not lyr or not lyr.isValid():
            return
        if lyr.fields().indexFromName("fix_time") < 0:
            return   # nothing to cull/sort on

        records = []   # (fid, device_id, fix_time_iso_str)
        for f in lyr.getFeatures():
            ft = f["fix_time"]
            if ft is None or ft == NULL:
                ft_str = ""
            elif hasattr(ft, "toString"):   # QDateTime
                ft_str = ft.toString("yyyy-MM-ddTHH:mm:ss")
            else:
                ft_str = str(ft)[:19]
            records.append((f.id(), f["device_id"], ft_str))
        if not records:
            return

        remove_ids: set = set()

        # ── Determine each device's newest record — always protected from
        # ── age-based culling, so every device retains at least one point.
        newest_fid_by_dev: dict = {}
        newest_ft_by_dev:  dict = {}
        for fid, dev_id, ft in records:
            if dev_id not in newest_ft_by_dev or ft > newest_ft_by_dev[dev_id]:
                newest_ft_by_dev[dev_id]  = ft
                newest_fid_by_dev[dev_id] = fid
        protected_fids = set(newest_fid_by_dev.values())

        # ── Cull by age ───────────────────────────────────────────────────
        if self.cull_by_age and self.cull_age_minutes > 0:
            cutoff_iso = (datetime.now(timezone.utc) -
                          timedelta(minutes=self.cull_age_minutes)
                          ).strftime("%Y-%m-%dT%H:%M:%SZ")
            for fid, dev_id, ft in records:
                if ft and ft < cutoff_iso and fid not in protected_fids:
                    remove_ids.add(fid)

        # ── Cull by count (per device, newest kept first) ──────────────────
        if self.cull_by_count:
            by_dev: dict = {}
            for rec in records:
                if rec[0] in remove_ids:
                    continue   # already marked for removal by age cull
                by_dev.setdefault(rec[1], []).append(rec)
            for arr in by_dev.values():
                arr.sort(key=lambda r: r[2], reverse=True)   # newest first
                if len(arr) > self.cull_max_per_device:
                    for fid, _dev, _ft in arr[self.cull_max_per_device:]:
                        remove_ids.add(fid)

        if not remove_ids:
            return

        ok = lyr.dataProvider().deleteFeatures(list(remove_ids))
        if ok:
            lyr.triggerRepaint()
        else:
            self._warn("Layer B cull: failed to delete features.")

    # ── C: line tracks ────────────────────────────────────────────────────────

    def _append_to_line_layer(self, processed):
        """
        Each device has one line feature.  All positions in `processed` are
        appended as vertices in fix_time order — supports both single-position
        (normal mode) and multiple-positions-per-device (history mode).
        New devices get a starter feature that grows from the second fetch on.
        """
        lyr = QgsProject.instance().mapLayer(self.ln_layer_id)
        if not lyr or not lyr.isValid():
            self._warn("Line track layer not found — check Settings.")
            return

        # Group by device and sort each group by GPS fix time
        by_device: dict[int, list] = {}
        for p in processed:
            by_device.setdefault(p["device_id"], []).append(p)
        for pts in by_device.values():
            pts.sort(key=lambda x: x["fix_time"])

        geom_changes: dict = {}
        attr_changes: dict = {}
        new_features: list = []
        lu_idx = lyr.fields().indexFromName("last_update")

        for dev_id, pts in by_device.items():
            existing = list(lyr.getFeatures(f'"device_id" = {dev_id}'))

            if existing:
                feat     = existing[0]
                geom     = feat.geometry()
                # Read existing vertices as QgsPoint — preserves Z and M from
                # previous writes. Old 2D features get z=0, m=0 automatically.
                verts    = [QgsPoint(v.x(), v.y(), v.z(), v.m())
                            for v in geom.vertices()]
                orig_len = len(verts)
                for p in pts:
                    epoch  = _epoch_from_fix_time(p["fix_time"])
                    new_pt = QgsPoint(p["lon"], p["lat"],
                                      p.get("altitude", 0.0), epoch)
                    # Compare XY only — don't duplicate stationary heartbeats
                    if not verts or (verts[-1].x() != new_pt.x()
                                     or verts[-1].y() != new_pt.y()):
                        verts.append(new_pt)
                if len(verts) > orig_len:
                    geom_changes[feat.id()] = QgsGeometry.fromPolyline(verts)
                    if lu_idx >= 0:
                        attr_changes[feat.id()] = {lu_idx: _to_qdt(pts[-1]["fix_time"])}
            else:
                # New device — build full line from all positions in this batch
                verts = []
                for p in pts:
                    epoch  = _epoch_from_fix_time(p["fix_time"])
                    new_pt = QgsPoint(p["lon"], p["lat"],
                                      p.get("altitude", 0.0), epoch)
                    if not verts or (verts[-1].x() != new_pt.x()
                                     or verts[-1].y() != new_pt.y()):
                        verts.append(new_pt)
                if len(verts) == 1:
                    verts.append(verts[0])   # degenerate until second fetch
                f = QgsFeature(lyr.fields())
                f.setGeometry(QgsGeometry.fromPolyline(verts))
                for field in lyr.fields():
                    fn = field.name()
                    if   fn == "device_id":   f[fn] = dev_id
                    elif fn == "name":        f[fn] = pts[0]["name"]
                    elif fn == "start_time":  f[fn] = _to_qdt(pts[0]["fix_time"])
                    elif fn == "last_update": f[fn] = _to_qdt(pts[-1]["fix_time"])
                new_features.append(f)

        dp = lyr.dataProvider()
        if geom_changes:  dp.changeGeometryValues(geom_changes)
        if attr_changes:  dp.changeAttributeValues(attr_changes)
        if new_features:  dp.addFeatures(new_features)
        lyr.triggerRepaint()
        lyr.updateExtents()
        _sync_categorized_renderer(lyr, {p["name"] for p in processed})

    # ── Utility ───────────────────────────────────────────────────────────────

    def _warn(self, msg):
        self.iface.messageBar().pushMessage(
            "Traccar Live", msg, level=_MSG_CRIT, duration=8)
        QgsMessageLog.logMessage(msg, "Traccar Live", _MSG_CRIT)
