"""
Traccar Live – QGIS Plugin  v0.2

Follows Traccar devices on the QGIS map within one chosen time window, and saves
their positions and tracks to two project layers on demand. Mirrors the QField
plugin Traccar_QField v0.4, and uses the same layer schema, so one GeoPackage
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

Unobtrusive by design: the main window is a non-modal pop-up that can be closed
while Live runs (toolbar button ▶ Live starts/stops it), network requests are
asynchronous, and Live errors are reported once per error streak.

Compatible with QGIS 3.28 LTR … 3.44 (Qt5 / PyQt5) and QGIS 4.x (Qt6 / PyQt6).
"""

import json
import os
import base64
import hashlib
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as _quote

from qgis.PyQt.QtCore import QTimer, QSettings, QDateTime, QDate, QTime, QUrl, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDateTimeEdit, QComboBox, QCheckBox,
    QRadioButton, QButtonGroup, QPushButton, QDialogButtonBox, QTabWidget,
    QWidget, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QFileDialog, QMessageBox, QTextBrowser, QToolButton,
)
from qgis.core import (
    Qgis, QgsProject, QgsVectorLayer, QgsField, QgsFields, QgsFeature,
    QgsFeatureRequest, QgsGeometry, QgsPoint, QgsPointXY, QgsLineString,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsRectangle,
    QgsVectorFileWriter, QgsVectorLayerUtils, QgsWkbTypes, QgsMessageLog,
    QgsNetworkAccessManager, QgsMapLayerProxyModel, QgsFieldProxyModel,
    QgsMarkerSymbol, QgsLineSymbol, QgsSimpleMarkerSymbolLayer, QgsSymbolLayer,
    QgsSingleSymbolRenderer, QgsProperty, QgsUnitTypes,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
)
from qgis.gui import QgsMapLayerComboBox, QgsFieldComboBox

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


_GEOM_POINT   = _new_or_old(getattr(Qgis, "GeometryType", None), "Point", QgsWkbTypes, "PointGeometry")
_GEOM_LINE    = _new_or_old(getattr(Qgis, "GeometryType", None), "Line",  QgsWkbTypes, "LineGeometry")
_LF_POINT     = _new_or_old(getattr(Qgis, "LayerFilter", None), "PointLayer",
                            getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "PointLayer")
_LF_LINE      = _new_or_old(getattr(Qgis, "LayerFilter", None), "LineLayer",
                            getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "LineLayer")
_LF_VECTOR    = _new_or_old(getattr(Qgis, "LayerFilter", None), "VectorLayer",
                            getattr(QgsMapLayerProxyModel, "Filter", QgsMapLayerProxyModel), "VectorLayer")
_FF_STRING    = _e(QgsFieldProxyModel, "Filter", "String")
_RENDER_M     = _new_or_old(getattr(Qgis, "RenderUnit", None), "MetersInMapUnits",
                            QgsUnitTypes, "RenderMetersInMapUnits")
_MSG_INFO     = _e(Qgis, "MessageLevel", "Info")
_MSG_WARN     = _e(Qgis, "MessageLevel", "Warning")
_MSG_OK       = _e(Qgis, "MessageLevel", "Success")
_HTTP_STATUS  = _e(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
_NET_OK       = _e(QNetworkReply, "NetworkError", "NoError")
_BTN_OK       = _e(QDialogButtonBox, "StandardButton", "Ok")
_BTN_CANCEL   = _e(QDialogButtonBox, "StandardButton", "Cancel")
_DLG_OK       = _e(QDialog, "DialogCode", "Accepted")
_ECHO_PWD     = _e(QLineEdit, "EchoMode", "Password")
_NO_EDIT      = _e(QAbstractItemView, "EditTrigger", "NoEditTriggers")
_SEL_ROWS     = _e(QAbstractItemView, "SelectionBehavior", "SelectRows")
_HV_STRETCH   = _e(QHeaderView, "ResizeMode", "Stretch")
_HV_CONTENTS  = _e(QHeaderView, "ResizeMode", "ResizeToContents")
_VFW_OK       = _e(QgsVectorFileWriter, "WriterError", "NoError")
_GPKG_NEW     = _e(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
_GPKG_LAYER   = _e(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")


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

SETTINGS_NS   = "TraccarLive"
MENU_LABEL    = "&Traccar Live"
DEFAULT_URL   = "https://server.traccar.org"
GROUP_NAME    = "Traccar (live)"
OVERLAY_PROP  = "traccar_overlay"
WGS84         = QgsCoordinateReferenceSystem("EPSG:4326")

# Time window choices: minutes > 0 = moving "Last …", -1 = custom dates, -2 = from feature
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
    "server_url": DEFAULT_URL, "username": "", "password": "",
    "window_minutes": 60, "custom_from": "", "custom_to": "",
    "event_layer_id": "", "event_display_field": "", "event_start_field": "",
    "event_end_field": "", "event_feature_fid": -1, "feature_span": 0, "feature_duration": 120,
    "live_interval_s": 30, "stale_minutes": 10,
    "show_markers": True, "show_labels": True, "show_tracks": True, "show_accuracy": False,
    "points_layer_id": "", "points_name_field": "", "points_mode": 0,
    "tracks_layer_id": "", "tracks_name_field": "", "tracks_mode": 0,
    "tag_enabled": False, "tag_text": "", "tag_field": "", "tag_from_feature": False,
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


def _device_color(name):
    """Deterministic colour from the device name — same name, same colour."""
    hue = int(hashlib.md5(str(name).encode()).hexdigest()[:4], 16) % 360
    return QColor.fromHsv(hue, 190, 200).name()


# ════════════════════════════════════════════════════════════════════════════
#  Asynchronous Traccar API client (QGIS network manager → proxy settings apply,
#  QGIS never freezes). Accept: application/json stops Traccar sending a
#  WWW-Authenticate challenge, so a wrong password never pops up a login box.
# ════════════════════════════════════════════════════════════════════════════

class _Api:
    def __init__(self, cfg):
        self.cfg      = cfg
        self._pending = set()

    def get(self, path, on_ok, on_err):
        url = self.cfg["server_url"].rstrip("/") + path
        req = QNetworkRequest(QUrl(url))
        token = base64.b64encode(
            ("%s:%s" % (self.cfg["username"], self.cfg["password"])).encode("utf-8")).decode()
        req.setRawHeader(b"Authorization", ("Basic " + token).encode())
        req.setRawHeader(b"Accept", b"application/json")
        try:
            req.setTransferTimeout(30000)
        except AttributeError:     # pragma: no cover
            pass
        reply = QgsNetworkAccessManager.instance().get(req)
        self._pending.add(reply)

        def finished():
            self._pending.discard(reply)
            status = reply.attribute(_HTTP_STATUS)
            err    = reply.error()
            errstr = reply.errorString()
            data   = bytes(reply.readAll())
            reply.deleteLater()
            if err == _NET_OK and (status is None or int(status) == 200):
                try:
                    obj = json.loads(data.decode("utf-8"))
                except Exception as exc:
                    on_err("Unexpected response from server: %s" % exc)
                    return
                on_ok(obj)
            elif status:
                status = int(status)
                on_err("Wrong username or password (HTTP 401)" if status == 401
                       else "HTTP %d on %s" % (status, path.split("?")[0]))
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
#  GeoPackage template (same tables as Traccar_QField's template)
# ════════════════════════════════════════════════════════════════════════════

def _memory_uri(geom, schema, crs="EPSG:4326"):
    return geom + "?crs=" + crs + "".join("&field=%s:%s" % f for f in schema)


def default_gpkg_path():
    """Suggested file for New GeoPackage: traccar.gpkg in the project's home folder
    (else the last folder used, else the user's home), never an existing file."""
    folder = QgsProject.instance().homePath()         or QSettings().value(SETTINGS_NS + "/last_gpkg_dir", "", type=str)         or os.path.expanduser("~")
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
#  Main window (non-modal pop-up)
# ════════════════════════════════════════════════════════════════════════════

class MainDialog(QDialog):
    def __init__(self, plugin, parent):
        super().__init__(parent)
        self.p = plugin
        self.setWindowTitle("Traccar Live")
        self.setModal(False)
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)

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
        self.to_edit   = QDateTimeEdit()
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
        root.addLayout(row)

        # ── What to show ──────────────────────────────────────────────────
        show = QHBoxLayout()
        show.addWidget(QLabel("Show:"))
        self.chk_markers  = QCheckBox("Markers")
        self.chk_labels   = QCheckBox("Labels")
        self.chk_tracks   = QCheckBox("Tracks")
        self.chk_accuracy = QCheckBox("Accuracy")
        for c in (self.chk_markers, self.chk_labels, self.chk_tracks, self.chk_accuracy):
            show.addWidget(c)
        show.addStretch()
        root.addLayout(show)

        # ── Devices ───────────────────────────────────────────────────────
        head = QHBoxLayout()
        self.devices_lbl = QLabel("Devices")
        self.devices_lbl.setStyleSheet("font-weight:bold;")
        self.updated_lbl = QLabel()
        self.updated_lbl.setStyleSheet("color:#777;")
        self.zoom_all_btn = QToolButton()
        self.zoom_all_btn.setText("Zoom to all")
        head.addWidget(self.devices_lbl)
        head.addStretch()
        head.addWidget(self.updated_lbl)
        head.addWidget(self.zoom_all_btn)
        root.addLayout(head)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Device", "Fixes", "Time span", "Speed", "Battery"])
        self.table.setEditTriggers(_NO_EDIT)
        self.table.setSelectionBehavior(_SEL_ROWS)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, _HV_STRETCH)
        for c in range(1, 5):
            hh.setSectionResizeMode(c, _HV_CONTENTS)
        self.table.setToolTip("Double-click a device to centre the map on it")
        self.table.setMinimumHeight(140)
        root.addWidget(self.table, 1)

        # ── Save ──────────────────────────────────────────────────────────
        save = QHBoxLayout()
        self.save_pos_btn = QPushButton("📍  Save positions")
        self.save_trk_btn = QPushButton("〰  Save tracks")
        save.addWidget(self.save_pos_btn)
        save.addWidget(self.save_trk_btn)
        root.addLayout(save)
        self.saves_to_lbl = QLabel()
        self.saves_to_lbl.setWordWrap(True)
        self.saves_to_lbl.setTextFormat(_e(Qt, "TextFormat", "RichText"))
        root.addWidget(self.saves_to_lbl)

        # ── Footer ────────────────────────────────────────────────────────
        foot = QHBoxLayout()
        self.settings_btn = QPushButton("Settings…")
        self.help_btn = QPushButton("Help")
        close_btn = QPushButton("Close")
        foot.addWidget(self.settings_btn)
        foot.addWidget(self.help_btn)
        foot.addStretch()
        foot.addWidget(close_btn)
        root.addLayout(foot)

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
        self.table.cellDoubleClicked.connect(self._zoom_row)
        self.zoom_all_btn.clicked.connect(self.p.zoom_to_all)
        self.save_pos_btn.clicked.connect(self.p.save_positions)
        self.save_trk_btn.clicked.connect(self.p.save_tracks)
        self.saves_to_lbl.linkActivated.connect(lambda _l: self.p.open_settings(1))
        self.settings_btn.clicked.connect(lambda: self.p.open_settings(0))
        self.help_btn.clicked.connect(self.p.show_help)
        close_btn.clicked.connect(self.close)

        self._rows = []

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
                         (self.chk_tracks, "show_tracks"), (self.chk_accuracy, "show_accuracy")):
            chk.blockSignals(True)
            chk.setChecked(bool(c[key]))
            chk.blockSignals(False)
        if c["window_minutes"] == -2:
            self.reload_features()
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
        self.devices_lbl.setText("Devices  (%d)" % len(rows))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [row["name"], row["fixes"], row["span"], row["speed"], row["battery"]]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if col == 0:
                    item.setForeground(QColor(row["color"] if row["fresh"] else "#9E9E9E"))
                    item.setToolTip(row["tip"])
                self.table.setItem(r, col, item)
        self.saves_to_lbl.setText(self.p.saves_to_html())

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
        if 0 <= r < len(self._rows) and self._rows[r]["pos"] is not None:
            self.p.zoom_to(self._rows[r]["pos"])

    def closeEvent(self, ev):
        QSettings().setValue(SETTINGS_NS + "/main_geometry", self.saveGeometry())
        super().closeEvent(ev)


# ════════════════════════════════════════════════════════════════════════════
#  Settings (tabs: Connection, Layers, Tag, Advanced)
# ════════════════════════════════════════════════════════════════════════════

class SettingsDialog(QDialog):
    def __init__(self, plugin, parent, tab=0):
        super().__init__(parent)
        self.p = plugin
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
        self.pass_edit = QLineEdit(c["password"])
        self.pass_edit.setEchoMode(_ECHO_PWD)
        f.addRow("Server URL:", self.url_edit)
        f.addRow("Email / username:", self.user_edit)
        f.addRow("Password:", self.pass_edit)
        test = QPushButton("Test connection")
        self.test_lbl = QLabel()
        self.test_lbl.setWordWrap(True)
        f.addRow(test)
        f.addRow(self.test_lbl)
        hint = QLabel("Use the address you open in a browser for Traccar. "
                      "The password is stored in your QGIS user settings.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#666;")
        f.addRow(hint)
        test.clicked.connect(self._test)
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
        self.pt_every  = QRadioButton("Save every fix in the time window")
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
        self.ln_add  = QRadioButton("Add a new track for each device on every save")
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

        g = QGroupBox("Time window 'From feature'")
        gf = QFormLayout(g)
        self.ev_combo = QgsMapLayerComboBox()
        self.ev_combo.setFilters(_LF_VECTOR)
        self.ev_combo.setAllowEmptyLayer(True)
        self.ev_disp  = QgsFieldComboBox()
        self.ev_start = QgsFieldComboBox()
        self.ev_end   = QgsFieldComboBox()
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
        cfg.update(server_url=self.url_edit.text().strip().rstrip("/"),
                   username=self.user_edit.text().strip(), password=self.pass_edit.text())
        api = _Api(cfg)
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
<h3>Live, Refresh and Clear</h3>
<p><b>▶ Live</b> refreshes in the background and keeps a “Last …” window moving. You can close this
window — Live keeps running; the <b>▶ Live</b> toolbar button starts and stops it. A window that ends
in the past cannot change, so Live pauses for it and the markers show each device's last fix in it.
<b>↻ Refresh</b> loads the window once; <b>Clear</b> stops Live and removes the overlay.</p>
<h3>On the map</h3>
<p>The live view is two temporary layers in the <i>Traccar (live)</i> group, <i>Temp Markers</i> and
<i>Temp Tracks</i> — separate from the layers you save to (e.g. <i>Traccar Positions</i> and
<i>Traccar Tracks</i>). Each
device in its own colour (grey when its last fix is older than the limit in Settings → Advanced).
They are never written to file and QGIS won't ask to save them. Switch Markers, Labels, Tracks and
Accuracy circles on or off here or in the Layers panel; use Identify on them as on any layer.
Double-click a device in the list to centre the map on it.</p>
<h3>Saving</h3>
<p><b>📍 Save positions</b> adds a point per device — its latest fix, or every fix in the window.<br>
<b>〰 Save tracks</b> adds one line per device for the window (Z = altitude, M = time), or replaces
that device's earlier track if <i>Keep only the most recent</i> is chosen.<br>
Fields are filled by name; fields your layer doesn't have are skipped. The device name can also
go into any text field (e.g. <i>title</i>). If the layer is already being edited, the features are
added to that edit session for you to save.</p>
<h3>Times</h3>
<p>Times here are local, including summer time. Date/time fields are stored in UTC (QGIS shows them
as UTC); the optional text fields <i>fix_local</i>, <i>start_local</i> and <i>last_local</i> hold the
local time. For a local date/time in labels use <code>datetime_from_epoch(epoch("fix_time"))</code>.</p>
<h3>Tag</h3>
<p>Settings → Tag stamps a text such as FIRE-2026-001 onto everything you save. With
<i>From feature</i>, the feature's display value can be used instead.</p>
<p>Same layers and fields as the QField plugin <i>Traccar_QField</i>, so one GeoPackage works in both.</p>
"""


# ════════════════════════════════════════════════════════════════════════════
#  Plugin
# ════════════════════════════════════════════════════════════════════════════

class TraccarLive:

    def __init__(self, iface):
        self.iface   = iface
        self.cfg     = {}
        self._load_settings()
        self.api     = _Api(self.cfg)
        self.timer   = QTimer()
        self.timer.timeout.connect(self.poll_live)
        self._actions = []
        self.dlg      = None

        # Runtime state
        self.live         = False
        self.loading      = False
        self.load_started = 0.0
        self.devices      = {}      # dev_id → {name, status}
        self.win          = None    # see _compute_window()
        self.tracks       = {}      # dev_id → [positions in window], oldest first
        self.latest       = {}      # dev_id → latest position
        self.marker_pos   = {}      # dev_id → position drawn as the marker
        self.device_rows  = []
        self.last_update  = ""
        self.status_msg   = ""
        self.last_error   = ""
        self._mk_id = ""            # overlay layer ids
        self._tk_id = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────
    def initGui(self):
        mw = self.iface.mainWindow()
        self.act_open = QAction("Traccar Live", mw)
        self.act_open.setToolTip("Traccar Live — time window, devices and saving")
        self.act_open.triggered.connect(self.open_main)
        self.act_live = QAction("▶ Live", mw)
        self.act_live.setCheckable(True)
        self.act_live.setToolTip("Traccar Live: start / stop live updates")
        self.act_live.toggled.connect(self.set_live)
        self.act_settings = QAction("Settings…", mw)
        self.act_settings.triggered.connect(lambda: self.open_settings(0))
        self.act_help = QAction("Help", mw)
        self.act_help.triggered.connect(self.show_help)
        for act in (self.act_open, self.act_live):
            self.iface.addToolBarIcon(act)
        for act in (self.act_open, self.act_live, self.act_settings, self.act_help):
            self.iface.addPluginToMenu(MENU_LABEL, act)
            self._actions.append(act)
        QgsProject.instance().readProject.connect(self._on_project_changed)
        QgsProject.instance().cleared.connect(self._on_project_changed)
        self._remove_stale_overlays()

    def unload(self):
        self.timer.stop()
        self.api.abort_all()
        for sig in (QgsProject.instance().readProject, QgsProject.instance().cleared):
            try:
                sig.disconnect(self._on_project_changed)
            except (TypeError, RuntimeError):
                pass
        self._remove_overlays()
        if self.dlg is not None:
            self.dlg.close()
            self.dlg.deleteLater()
            self.dlg = None
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

    def set_cfg(self, key, value):
        if value is None:
            return
        self.cfg[key] = value
        QSettings().setValue("%s/%s" % (SETTINGS_NS, key), value)

    # ── Windows ───────────────────────────────────────────────────────────
    def open_main(self):
        if self.dlg is None:
            self.dlg = MainDialog(self, self.iface.mainWindow())
            geo = QSettings().value(SETTINGS_NS + "/main_geometry")
            if geo:
                self.dlg.restoreGeometry(geo)
        self.dlg.load_state()
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()

    def open_settings(self, tab=0):
        dlg = SettingsDialog(self, self.iface.mainWindow(), tab)
        if dlg.exec() == _DLG_OK:
            vals = dlg.values()
            interval_changed = vals["live_interval_s"] != self.cfg["live_interval_s"]
            for k, v in vals.items():
                self.cfg[k] = v
            self._save_settings()
            if interval_changed and self.timer.isActive():
                self.timer.start(int(self.cfg["live_interval_s"]) * 1000)
            self._rebuild()
            if self.dlg is not None and self.dlg.isVisible():
                self.dlg.load_state()

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

    def _ui(self):
        if self.dlg is not None and self.dlg.isVisible():
            self.dlg.refresh()
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
        if self.live:
            self.set_live(False)
        self.api.abort_all()
        self.loading = False
        self.win = None
        self.tracks, self.latest, self.marker_pos = {}, {}, {}
        self.status_msg = ""
        self._remove_overlays()
        self._rebuild()

    def reload_window(self):
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
            to  = _parse_local(self.cfg["custom_to"])
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
            disp  = val(self.cfg["event_display_field"])
            disp  = "" if _is_null(disp) else str(disp)
            start = _attr_to_utc(val(self.cfg["event_start_field"]))
            end   = _attr_to_utc(val(self.cfg["event_end_field"]))
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
        self.status_msg = "Loading…"
        self._ui()

        def fail(msg):
            self.loading = False
            self.status_msg = ""
            self._error(msg)

        def got_devices(devs):
            self.devices = {d["id"]: {"name": d.get("name") or str(d["id"]),
                                      "status": d.get("status") or ""} for d in devs}

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
        if not self.win["moving"]:
            return

        def fail(msg):
            self._error(msg)

        def got_devices(devs):
            self.devices = {d["id"]: {"name": d.get("name") or str(d["id"]),
                                      "status": d.get("status") or ""} for d in devs}

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

        rows = []
        for dev_id, info in self.devices.items():
            tr  = self.tracks.get(dev_id, [])
            pos = mp.get(dev_id)
            attrs = (pos or {}).get("attributes") or {}
            bat = attrs.get("batteryLevel")
            rows.append({
                "name":    info["name"],
                "fixes":   len(tr) if tr else "—",
                "span":    (_span_text(tr[0]["_t"], tr[-1]["_t"]) if len(tr) > 1
                            else _fmt_local(tr[0]["_t"]) if tr else "no fixes in window"),
                "speed":   ("%d km/h" % round((pos.get("speed") or 0) * 1.852)) if pos else "",
                "battery": ("%g%%" % float(bat)) if isinstance(bat, (int, float)) else "",
                "pos":     pos,
                "fresh":   self._fresh(pos["_t"]) if pos else False,
                "color":   _device_color(info["name"]),
                "tip":     ("Last fix %s ago" % _age_text(pos["_t"])) if pos else "No position",
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
        root  = QgsProject.instance().layerTreeRoot()
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
        mk = QgsProject.instance().mapLayer(self._mk_id) if self._mk_id else None
        if key == "show_labels" and mk is not None:
            mk.setLabelsEnabled(bool(on))
            mk.triggerRepaint()
        elif key == "show_accuracy" and mk is not None:
            self._style_markers(mk)
        else:
            self._apply_visibility()

    def _update_overlays(self):
        mk = self._overlay("markers")
        feats = []
        for dev_id, p in self.marker_pos.items():
            f = QgsFeature(mk.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p["longitude"], p["latitude"])))
            attrs = p.get("attributes") or {}
            name = self._name(dev_id)
            f.setAttributes([dev_id, name, _qdt(p["_t"]), _local_text(p["_t"]),
                             round((p.get("speed") or 0) * 1.852, 1), attrs.get("batteryLevel"),
                             p.get("accuracy") or 0.0, 1 if self._fresh(p["_t"]) else 0,
                             _device_color(name)])
            feats.append(f)
        mk.dataProvider().truncate()
        mk.dataProvider().addFeatures(feats)
        mk.updateExtents()
        mk.triggerRepaint()

        tk = self._overlay("tracks")
        feats = []
        for dev_id, pts in self.tracks.items():
            if len(pts) < 2:
                continue
            line = QgsLineString([p["longitude"] for p in pts], [p["latitude"] for p in pts],
                                 [float(p.get("altitude") or 0) for p in pts],
                                 [p["_t"].timestamp() for p in pts])
            name = self._name(dev_id)
            f = QgsFeature(tk.fields())
            f.setGeometry(QgsGeometry(line))
            f.setAttributes([dev_id, name, len(pts), _local_text(pts[0]["_t"]),
                             _local_text(pts[-1]["_t"]), 1 if self._fresh(pts[-1]["_t"]) else 0,
                             _device_color(name)])
            feats.append(f)
        tk.dataProvider().truncate()
        tk.dataProvider().addFeatures(feats)
        tk.updateExtents()
        tk.triggerRepaint()

    def _remove_overlays(self):
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
        self._mk_id = self._tk_id = ""
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

    def zoom_to_all(self):
        pts = [p for v in self.tracks.values() for p in v] + list(self.marker_pos.values())
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

    def save_positions(self):
        lyr = self._target("points_layer_id", "positions")
        if lyr is None:
            return
        if self.cfg["points_mode"] == 1:
            positions = [p for v in self.tracks.values() for p in v]
        else:
            positions = list(self.marker_pos.values())
        if not positions:
            self._bar("No positions to save", _MSG_WARN)
            return
        n, note = self._write_points(lyr, positions)
        if n >= 0:
            self._bar("Saved %d point(s) to %s%s" % (n, lyr.name(), note), _MSG_OK)

    def save_tracks(self):
        lyr = self._target("tracks_layer_id", "tracks")
        if lyr is None:
            return
        by_dev = {k: v for k, v in self.tracks.items() if v}
        if not by_dev:
            self._bar("No fixes in this window to save", _MSG_WARN)
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
        names  = fields.names()
        name_field = self._name_field(names, self.cfg["points_name_field"])
        tr     = QgsCoordinateTransform(WGS84, lyr.crs(), QgsProject.instance())
        has_z  = QgsWkbTypes.hasZ(lyr.wkbType())
        saved  = datetime.now(timezone.utc)
        tag    = self.win["tag"] if self.win else ""
        feats  = []
        for p in positions:
            dev_id = p.get("deviceId")
            info   = self.devices.get(dev_id, {})
            attrs  = p.get("attributes") or {}
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
        names  = fields.names()
        name_field = self._name_field(names, self.cfg["tracks_name_field"])
        w      = self.win

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

        tr    = QgsCoordinateTransform(WGS84, lyr.crs(), QgsProject.instance())
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
