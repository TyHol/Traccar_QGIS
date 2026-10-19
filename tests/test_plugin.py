# Copyright (C) 2026 TyHol
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Integration tests for Traccar Live (QGIS plugin), run inside a real QGIS Python.

    QT_QPA_PLATFORM=offscreen  python-qgis-ltr.bat tests/test_plugin.py   (QGIS 3.x)
    QT_QPA_PLATFORM=offscreen  python-qgis.bat     tests/test_plugin.py   (QGIS 4.x)

A fake Traccar server (127.0.0.1, random port) serves three devices:
  1 "Phone A" — a fix every 30 s for the last 2 hours (moving)
  2 "Van 3"   — 20 fixes two days ago, nothing since
  3 "Spare"   — no fixes at all
Settings go to a temporary INI, never to the user's QGIS settings.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="traccar_qgis_test_")

import faulthandler  # noqa: E402
faulthandler.dump_traceback_later(300, exit=True)   # never hang: show where it is stuck and stop

from qgis.PyQt.QtCore import QSettings, QCoreApplication, QDateTime  # noqa: E402
QSettings.setDefaultFormat(QSettings.Format.IniFormat if hasattr(QSettings, "Format") else QSettings.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat if hasattr(QSettings, "Format") else QSettings.IniFormat,
                  QSettings.Scope.UserScope if hasattr(QSettings, "Scope") else QSettings.UserScope, TMP)

from qgis.testing import start_app            # noqa: E402
from qgis.testing.mocked import get_iface     # noqa: E402
from qgis.core import (                       # noqa: E402
    Qgis, QgsProject, QgsVectorLayer, QgsVectorFileWriter, QgsWkbTypes, QgsFeature,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PKG = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fake Traccar server ──────────────────────────────────────────────────────
NOW = datetime.now(timezone.utc).replace(microsecond=0)
USER, PWD = "test@example.com", "secret"  # pragma: allowlist secret  (fake server)
DEVICES = [{"id": 1, "name": "Phone A", "status": "online"},
           {"id": 2, "name": "Van 3", "status": "offline"},
           {"id": 3, "name": "Spare", "status": "unknown"}]


def _fix(dev, t, lon, lat, i):
    return {"id": dev * 100000 + i, "deviceId": dev, "fixTime": t.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            "latitude": lat, "longitude": lon, "altitude": 90.0 + i % 5, "speed": 2.5,
            "course": 45.0, "accuracy": 6.0, "address": None,
            "attributes": {"batteryLevel": 81, "motion": True}}


W3W_KEY, W3W_CALLS = "w3w-test-key", []   # pragma: allowlist secret  (fake what3words API)
POS = {1: [], 2: [], 3: []}
for i in range(240):                                   # last 2 h, every 30 s
    t = NOW - timedelta(seconds=30 * (239 - i))
    POS[1].append(_fix(1, t, -6.33 + i * 0.0001, 53.355 + i * 0.00005, i))
VAN_START = NOW - timedelta(days=2)
for i in range(20):                                    # two days ago, 10 minutes
    POS[2].append(_fix(2, VAN_START + timedelta(seconds=30 * i), -6.25 + i * 0.0002, 53.35, i))


def _p(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        import base64
        if self.path.startswith("/v3/convert-to-3wa"):          # fake what3words API
            W3W_CALLS.append(self.path)
            if self.headers.get("X-Api-Key") != W3W_KEY:
                self._json(401, {"error": {"code": "InvalidKey", "message": "Authentication failed; invalid API key"}})
            else:
                self._json(200, {"words": "filled.count.soap"})
            return
        auth = self.headers.get("Authorization", "")
        if auth != "Basic " + base64.b64encode(("%s:%s" % (USER, PWD)).encode()).decode():
            self.send_response(401)
            self.end_headers()
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/devices":
            body = DEVICES
        elif u.path == "/api/positions" and "deviceId" in q:
            d = int(q["deviceId"][0])
            frm, to = _p(q["from"][0]), _p(q["to"][0])
            body = [p for p in POS.get(d, []) if frm <= _p(p["fixTime"]) <= to]
        elif u.path == "/api/positions":
            body = [v[-1] for v in POS.values() if v]
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % server.server_address[1]

# ── Harness ──────────────────────────────────────────────────────────────────
start_app()
from qgis.core import QgsApplication  # noqa: E402
QgsApplication.authManager().setMasterPassword("test-master", True)   # temporary auth DB of the test app
IFACE = get_iface()
RESULTS = []


def flush_deletes():
    from qgis.PyQt.QtCore import QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete
                                      if hasattr(QEvent, "Type") else QEvent.DeferredDelete)


def wait(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


def test(fn):
    try:
        fn()
    except Exception:
        RESULTS.append((fn.__name__, False, traceback.format_exc().strip().splitlines()[-1]))
        traceback.print_exc()
    return fn


mod = __import__(PKG + ".traccar_live", fromlist=["*"])
plugin = __import__(PKG, fromlist=["classFactory"]).classFactory(IFACE)
plugin.initGui()
plugin.set_login(URL, USER, "wrong")


def load(minutes=None):
    if minutes is not None:
        plugin.cfg["window_minutes"] = minutes
    plugin.reload_window()
    return wait(lambda: not plugin.loading)


def feats(lyr):
    return list(lyr.getFeatures())


def add(lyr):
    QgsProject.instance().addMapLayer(lyr)
    return lyr


# ── Tests ────────────────────────────────────────────────────────────────────

@test
def t01_wrong_password():
    load(180)
    check("wrong password → one clear error, nothing loaded",
          "401" in plugin.last_error and plugin.win is None, plugin.last_error)


@test
def t02_load_last_3h():
    plugin.set_login(URL, USER, PWD)
    ok = load(180)
    check("load finishes", ok)
    check("3 devices", len(plugin.devices) == 3, len(plugin.devices))
    check("Phone A: 240 fixes in last 3 h", len(plugin.tracks.get(1, [])) == 240,
          len(plugin.tracks.get(1, [])))
    check("Van 3 / Spare: no fixes in window", 2 not in plugin.tracks and 3 not in plugin.tracks)
    check("moving window → markers at latest fix (Phone A + Van 3)",
          set(plugin.marker_pos) == {1, 2}, sorted(plugin.marker_pos))
    rows = {r["name"]: r for r in plugin.device_rows}
    check("device list: 3 rows, Spare says 'no fixes'",
          len(rows) == 3 and rows["Spare"]["span"] == "no fixes in window")
    check("Phone A fresh, Van 3 stale", rows["Phone A"]["fresh"] and not rows["Van 3"]["fresh"])


@test
def t02b_drawn_on_map_by_default():
    from qgis.PyQt.QtGui import QImage, QPainter
    prj = QgsProject.instance()
    check("default live view = drawn on the map", plugin.cfg["live_view"] == "canvas")
    check("drawn on map: no Temp layers, no group",
          not plugin._mk_id and prj.layerTreeRoot().findGroup(mod.GROUP_NAME) is None)
    canvas = IFACE.mapCanvas()
    item = plugin._item
    check("drawn on map: one canvas item with 2 markers + 1 track",
          item is not None and item in canvas.scene().items()
          and len(item.markers) == 2 and len(item.tracks) == 1)

    win = IFACE.mainWindow()
    win.resize(500, 500)
    win.setCentralWidget(canvas)
    win.show()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:2157"))   # Irish Transverse Mercator
    wait(lambda: False, 0.3)
    plugin.zoom_to_all()
    wait(lambda: False, 0.3)

    fmt = QImage.Format.Format_ARGB32 if hasattr(QImage, "Format") else QImage.Format_ARGB32
    sz = canvas.viewport().size()
    img = QImage(sz.width(), sz.height(), fmt)
    img.fill(0)
    painter = QPainter(img)
    item.paint(painter)
    painter.end()
    drawn = sum(1 for x in range(0, img.width(), 2) for y in range(0, img.height(), 2)
                if (img.pixel(x, y) >> 24) & 0xFF)
    check("drawn on map: tracks and markers actually paint (map CRS EPSG:2157)", drawn > 200, drawn)

    m = [mm for mm in item.markers if mm["label"] == "Phone A"][0]
    q = canvas.getCoordinateTransform().transform(m["pt"])
    tip = plugin.marker_tip_at(q.x() + 4, q.y() - 3)
    check("hover over a marker → tooltip with name, age, speed, battery",
          tip.startswith("Phone A") and "ago" in tip and "km/h" in tip and "battery 81%" in tip, tip)
    check("hover away from markers → no tooltip", plugin.marker_tip_at(q.x() + 60, q.y() + 60) == "")

    plugin.set_show("show_labels", False)
    plugin.set_show("show_accuracy", True)
    check("toggles apply to the drawing", item.show_labels is False and item.show_accuracy is True)
    plugin.set_show("show_labels", True)
    plugin.set_show("show_accuracy", False)


@test
def t02c_switch_to_layers():
    plugin.set_live_view("layers")
    prj = QgsProject.instance()
    check("switch to temporary layers: canvas item removed, Temp layers added",
          plugin._item is None and prj.mapLayer(plugin._mk_id) is not None)


@test
def t03_overlay_layers():
    prj = QgsProject.instance()
    mk, tk = prj.mapLayer(plugin._mk_id), prj.mapLayer(plugin._tk_id)
    check("overlay layers exist in 'Traccar (live)' group",
          mk is not None and tk is not None and prj.layerTreeRoot().findGroup(mod.GROUP_NAME) is not None)
    check("overlay layers named Temp Markers / Temp Tracks",
          mk.name() == "Temp Markers" and tk.name() == "Temp Tracks", (mk.name(), tk.name()))
    check("overlay not saved / no scratch prompt",
          str(mk.customProperty("skipMemoryLayersCheck")) == "1" and mk.providerType() == "memory")
    check("markers: 2 features", mk.featureCount() == 2, mk.featureCount())
    lines = feats(tk)
    check("tracks: 1 line, 240 vertices", len(lines) == 1 and len(list(lines[0].geometry().vertices())) == 240)
    v0 = next(lines[0].geometry().vertices())
    check("track vertex M = fix time (epoch s)", abs(v0.m() - _p(POS[1][0]["fixTime"]).timestamp()) < 1,
          (v0.m(), POS[1][0]["fixTime"]))


@test
def t04_toggles():
    prj = QgsProject.instance()
    mk = prj.mapLayer(plugin._mk_id)
    plugin.set_show("show_labels", False)
    check("labels toggle", mk.labelsEnabled() is False)
    plugin.set_show("show_labels", True)
    plugin.set_show("show_accuracy", True)
    check("accuracy circles add a symbol layer", mk.renderer().symbol().symbolLayerCount() == 2)
    plugin.set_show("show_accuracy", False)
    plugin.set_show("show_markers", False)
    node = prj.layerTreeRoot().findLayer(plugin._mk_id)
    check("markers toggle hides layer", node is not None and not node.itemVisibilityChecked())
    plugin.set_show("show_markers", True)


@test
def t05_window_1h():
    load(60)
    n = len(plugin.tracks.get(1, []))
    check("Last 1 hour: ~120 fixes", 118 <= n <= 121, n)


GPKG = os.path.join(TMP, "traccar.gpkg")
GPKG_ITM = os.path.join(TMP, "traccar_irish_grid.gpkg")


@test
def t06_template_gpkg():
    pts, trk, err = mod.create_template_gpkg(GPKG)
    check("template GeoPackage: 2 layers, no errors", pts is not None and trk is not None and not err, err)
    check("tracks table is LineStringZM",
          QgsWkbTypes.hasZ(trk.wkbType()) and QgsWkbTypes.hasM(trk.wkbType()))
    add(pts).setName("pts")
    add(trk).setName("trk")
    plugin.cfg.update(points_layer_id=pts.id(), tracks_layer_id=trk.id(), points_mode=0, tracks_mode=0)


@test
def t06b_default_gpkg_in_project_home():
    home = os.path.join(TMP, "project home")
    os.makedirs(home, exist_ok=True)
    QgsProject.instance().setPresetHomePath(home)
    first = mod.default_gpkg_path()
    check("New GeoPackage suggests traccar.gpkg in the project home",
          first == os.path.join(home, "traccar.gpkg"), first)
    open(first, "w").close()
    second = mod.default_gpkg_path()
    check("existing traccar.gpkg is never offered again",
          second == os.path.join(home, "traccar_2.gpkg"), second)
    QgsProject.instance().setPresetHomePath("")


@test
def t07_save_latest_positions():
    lyr = QgsProject.instance().mapLayer(plugin.cfg["points_layer_id"])
    plugin.save_positions()
    fs = {f["name"]: f for f in feats(lyr)}
    check("latest fix per device → 2 points", len(fs) == 2, sorted(fs))
    a = fs["Phone A"]
    exp = POS[1][-1]
    qdt = a["fix_time"]
    got = datetime.fromtimestamp(qdt.toMSecsSinceEpoch() / 1000, timezone.utc) if isinstance(qdt, QDateTime) else None
    check("fix_time stored as UTC date/time", got is not None and abs((got - _p(exp["fixTime"])).total_seconds()) < 1,
          (qdt, exp["fixTime"]))
    check("fix_local text has offset", str(a["fix_local"]).count("UTC") == 1, a["fix_local"])
    check("device_id / battery / speed", a["device_id"] == 1 and a["battery"] == 81
          and abs(a["speed_kmh"] - 4.6) < 0.05)
    pt = a.geometry().asPoint()
    check("geometry = fix position", abs(pt.x() - exp["longitude"]) < 1e-7 and abs(pt.y() - exp["latitude"]) < 1e-7)
    check("layer not left in edit mode", not lyr.isEditable())


@test
def t08_save_every_fix():
    lyr = QgsProject.instance().mapLayer(plugin.cfg["points_layer_id"])
    before = lyr.featureCount()
    plugin.cfg["points_mode"] = 1
    plugin.save_positions()
    n = lyr.featureCount() - before
    check("every fix in window → one point per fix", n == len(plugin.tracks[1]), (n, len(plugin.tracks[1])))
    plugin.cfg["points_mode"] = 0


@test
def t09_save_tracks_add_then_keep():
    lyr = QgsProject.instance().mapLayer(plugin.cfg["tracks_layer_id"])
    plugin.save_tracks()
    plugin.save_tracks()
    check("add mode: 2 saves → 2 tracks", lyr.featureCount() == 2, lyr.featureCount())
    f = feats(lyr)[0]
    g = f.geometry()
    check("saved track keeps Z and M", QgsWkbTypes.hasZ(g.wkbType()) and QgsWkbTypes.hasM(g.wkbType()))
    check("n_points = fixes in window", f["n_points"] == len(plugin.tracks[1]), f["n_points"])
    st = f["start_time"]
    st = datetime.fromtimestamp(st.toMSecsSinceEpoch() / 1000, timezone.utc)
    check("start_time = first fix", abs((st - plugin.tracks[1][0]["_t"]).total_seconds()) < 1)
    plugin.cfg["tracks_mode"] = 1
    plugin.save_tracks()
    check("keep most recent: back to 1 track", lyr.featureCount() == 1, lyr.featureCount())
    plugin.cfg["tracks_mode"] = 0


@test
def t10_reprojection_irish_grid():
    pts, trk, err = mod.create_template_gpkg(GPKG_ITM, "EPSG:29903")
    add(pts)
    add(trk)
    plugin.cfg.update(points_layer_id=pts.id(), tracks_layer_id=trk.id())
    plugin.save_positions()
    plugin.save_tracks()
    back = QgsCoordinateTransform(pts.crs(), QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    f = [x for x in feats(pts) if x["device_id"] == 1][0]
    ll = back.transform(f.geometry().asPoint())
    exp = POS[1][-1]
    check("Irish Grid layer: point reprojected correctly",
          abs(ll.x() - exp["longitude"]) < 1e-5 and abs(ll.y() - exp["latitude"]) < 1e-5, (ll.x(), ll.y()))
    x0 = next(feats(trk)[0].geometry().vertices()).x()
    check("Irish Grid layer: track in metres, not degrees", x0 > 1000, x0)


@test
def t11_qfield_style_layer_title_and_tag():
    # Like QField's built-in Tracks layer: 2D LineString, a 'title' text field and 'tag', EPSG:2157
    path = os.path.join(TMP, "qfield_like.gpkg")
    tmp = QgsVectorLayer("LineString?crs=EPSG:2157&field=title:string(80)&field=tag:string(40)"
                         "&field=timestamp:datetime", "t", "memory")
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName, o.layerName = "GPKG", "tracks"
    QgsVectorFileWriter.writeAsVectorFormatV3(tmp, path, QgsProject.instance().transformContext(), o)
    lyr = add(QgsVectorLayer(path + "|layername=tracks", "Tracks", "ogr"))
    plugin.cfg.update(tracks_layer_id=lyr.id(), tracks_name_field="title", tracks_mode=1,
                      tag_enabled=True, tag_text="FIRE-1", tag_field="tag")
    plugin.save_tracks()
    plugin.save_tracks()
    fs = feats(lyr)
    check("2D layer: Z/M dropped, line saved", len(fs) == 1 and not QgsWkbTypes.hasM(fs[0].geometry().wkbType()))
    check("device name → title", fs and fs[0]["title"] == "Phone A", fs and fs[0]["title"])
    check("keep most recent matches by name (no device_id field)", len(fs) == 1, len(fs))
    check("tag written", fs and fs[0]["tag"] == "FIRE-1", fs and fs[0]["tag"])
    plugin.cfg.update(tag_enabled=False, tracks_mode=0, tracks_name_field="")


@test
def t12_edit_session_respected():
    lyr = QgsProject.instance().mapLayer(plugin.cfg["points_layer_id"])
    lyr.startEditing()
    before = lyr.featureCount()
    plugin.save_positions()
    check("layer already in edit mode: features go into the edit buffer, not committed",
          lyr.isEditable() and lyr.featureCount() == before + 2)
    lyr.rollBack()


@test
def t13_custom_past_window():
    def loc(d):
        return d.astimezone().strftime("%Y-%m-%d %H:%M")
    plugin.cfg.update(window_minutes=-1, custom_from=loc(VAN_START - timedelta(minutes=5)),
                      custom_to=loc(VAN_START + timedelta(minutes=30)))
    ok = load()
    w = plugin.win
    check("custom past window loads", ok and w is not None and not w["moving"])
    check("Van 3: 20 fixes, Phone A none", len(plugin.tracks.get(2, [])) == 20 and 1 not in plugin.tracks)
    check("past window → marker at last fix in window", set(plugin.marker_pos) == {2}
          and plugin.marker_pos[2]["id"] == POS[2][-1]["id"])


@test
def t14_from_feature_with_tag():
    ev = add(QgsVectorLayer("Point?crs=EPSG:4326&field=ref:string&field=start:datetime&field=end:datetime",
                            "incidents", "memory"))
    f = QgsFeature(ev.fields())
    f.setAttributes(["INC-7",
                     QDateTime.fromMSecsSinceEpoch(int((VAN_START - timedelta(minutes=1)).timestamp() * 1000)),
                     QDateTime.fromMSecsSinceEpoch(int((VAN_START + timedelta(minutes=20)).timestamp() * 1000))])
    ev.dataProvider().addFeatures([f])
    plugin.cfg.update(window_minutes=-2, event_layer_id=ev.id(), event_display_field="ref",
                      event_start_field="start", event_end_field="end", feature_span=0,
                      tag_from_feature=True, tag_enabled=True, tag_field="tag", tag_text="X")
    lst = plugin.event_features()
    plugin.cfg["event_feature_fid"] = lst[0]["fid"]
    check("feature list label", lst and lst[0]["label"].startswith("INC-7  ("), lst and lst[0]["label"])
    ok = load()
    check("from-feature window loads Van 3", ok and len(plugin.tracks.get(2, [])) == 20)
    check("feature display value becomes the tag", plugin.win and plugin.win["tag"] == "INC-7")
    trk = QgsProject.instance().mapLayer(plugin.cfg["tracks_layer_id"])
    plugin.save_tracks()
    last = sorted(feats(trk), key=lambda x: x.id())[-1]
    check("saved track tagged INC-7", last["tag"] == "INC-7", last["tag"])
    plugin.cfg.update(tag_enabled=False, tag_from_feature=False)


@test
def t15_live_poll_appends_and_trims():
    load(60)
    n0 = len(plugin.tracks[1])
    new = _fix(1, datetime.now(timezone.utc) + timedelta(seconds=1), -6.31, 53.365, 999)
    POS[1].append(new)
    plugin.poll_live()
    wait(lambda: len(plugin.tracks.get(1, [])) != n0, 5)
    check("Live: new fix appended", plugin.tracks[1][-1]["id"] == new["id"], len(plugin.tracks[1]))
    plugin.win["trim"] = 30          # pretend the window shrank → older fixes must drop
    plugin.poll_live()
    wait(lambda: len(plugin.tracks[1]) < 100, 5)
    n = len(plugin.tracks[1])
    check("Live: moving window drops fixes that fell out", 58 <= n <= 63, n)


@test
def t16_dialogs():
    plugin.cfg["window_minutes"] = 60
    plugin.open_main()
    d = plugin.dock
    wait(lambda: False, 0.2)
    from qgis.gui import QgsDockWidget
    check("main panel opens as a dock widget", isinstance(d, QgsDockWidget) and d.isVisible()
          and plugin.act_open.isChecked())
    check("device table filled", d.table.rowCount() == 3, d.table.rowCount())
    check("saves-to line shows layers", "Positions →" in d.saves_to_lbl.text())
    plugin.set_live(True)
    check("Live button and toolbar action in sync", d.live_btn.isChecked() and plugin.act_live.isChecked()
          and plugin.timer.isActive())
    plugin.act_open.setChecked(False)
    check("toolbar button hides the panel", not d.isUserVisible())
    check("closing the window keeps Live running", plugin.live and plugin.timer.isActive())
    plugin.set_live(False)
    from qgis.PyQt.QtWidgets import QDialog
    orig_exec = QDialog.exec
    QDialog.exec = lambda self: 0          # "Cancel" straight away
    try:
        before = len(IFACE.mainWindow().findChildren(mod.SettingsDialog))
        for _ in range(3):
            plugin.open_settings(1)
        flush_deletes()
        after = len(IFACE.mainWindow().findChildren(mod.SettingsDialog))
        check("Settings windows are deleted after closing (no leak)", after == before, (before, after))
    finally:
        QDialog.exec = orig_exec
    s = mod.SettingsDialog(plugin, IFACE.mainWindow(), 1)
    v = s.values()
    check("settings dialog round-trips values", v["points_layer_id"] == plugin.cfg["points_layer_id"]
          and v["server_url"] == URL and v["live_view"] == plugin.cfg["live_view"])
    s.deleteLater()
    flush_deletes()
    orig = QDialog.exec
    QDialog.exec = lambda self: 0
    try:
        plugin.show_help()
        check("help opens", True)
    finally:
        QDialog.exec = orig


@test
def t16b_per_device_show_hide():
    plugin.cfg["window_minutes"] = 180
    plugin.set_live_view("canvas")
    wait(lambda: not plugin.loading)           # the load started by ▶ Live in t16
    load(180)
    plugin.open_main()
    d = plugin.dock
    item = plugin._item
    r = [i for i, row in enumerate(d._rows) if row["name"] == "Phone A"][0]
    d.table.item(r, 0).setCheckState(mod._UNCHECKED)        # untick Phone A in the list
    wait(lambda: 1 in plugin.hidden_ids(), 2)
    check("unticking a device hides it on the map",
          [m["label"] for m in item.markers] == ["Van 3"] and not item.tracks, [m["label"] for m in item.markers])
    check("hidden devices remembered in the settings",
          QSettings().value("TraccarLive/hidden_devices") == "1", QSettings().value("TraccarLive/hidden_devices"))
    check("device list says how many are shown", "2 of 3 shown" in d.devices_lbl.text()
          and d.table.item(r, 0).checkState() == mod._UNCHECKED, d.devices_lbl.text())
    plugin.cfg["points_mode"] = 0
    lyr = QgsProject.instance().mapLayer(plugin.cfg["points_layer_id"])
    before = lyr.featureCount()
    plugin.save_positions()
    check("Save positions skips hidden devices", lyr.featureCount() - before == 1, lyr.featureCount() - before)
    plugin.save_positions(only=1)
    added = sorted(feats(lyr), key=lambda x: x.id())[-1]
    check("save one device (even hidden) from its menu", added["device_id"] == 1, added["device_id"])
    from qgis.PyQt.QtWidgets import QMenu
    shown = []
    orig_exec = QMenu.exec
    QMenu.exec = lambda self, *a: shown.extend(x.text() for x in self.actions())
    try:
        d._device_menu(r)
    finally:
        QMenu.exec = orig_exec
    check("⋯ menu: zoom, show only, show all, save to file, save to layers",
          any("Save to file" in t for t in shown) and any("Show only" in t for t in shown), shown)
    plugin.show_only(2)
    check("show only this device", plugin.hidden_ids() == {1, 3}, plugin.hidden_ids())
    check("'Devices' checkbox partial when some are hidden", d.devices_lbl.checkState() == mod._PARTIAL)
    d.devices_lbl.click()
    check("'Devices' checkbox: click → all shown", not plugin.hidden_ids()
          and d.devices_lbl.checkState() == mod._CHECKED, plugin.hidden_ids())
    d.devices_lbl.click()
    check("'Devices' checkbox: click again → none shown", plugin.hidden_ids() == {1, 2, 3}
          and d.devices_lbl.checkState() == mod._UNCHECKED and not plugin._item.markers, plugin.hidden_ids())
    plugin.show_all()
    check("show all devices", not plugin.hidden_ids() and len(plugin._item.markers) == 2,
          (plugin.hidden_ids(), [m["label"] for m in plugin._item.markers], plugin.play_t, plugin.loading))


@test
def t16c_export_device_files():
    import csv as _csv
    import xml.etree.ElementTree as ET  # nosec B405 - parses the file this test just wrote
    n = len(plugin.tracks[1])
    gpx = os.path.join(TMP, "phone a.gpx")
    ok, msg = plugin.export_device(1, gpx)
    root = ET.parse(gpx).getroot()  # nosec B314
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    pts = root.findall(".//g:trkpt", ns)
    check("GPX: one track with every fix, times and elevation", ok and len(pts) == n
          and pts[0].find("g:time", ns).text.endswith("Z") and pts[0].find("g:ele", ns) is not None, msg)
    check("GPX: waypoint at the last fix, named after the device",
          root.find("g:wpt/g:name", ns).text == "Phone A")
    csvp = os.path.join(TMP, "phone_a.csv")
    ok, msg = plugin.export_device(1, csvp)
    with open(csvp, encoding="utf-8-sig", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    check("CSV: one row per fix with lat/lon", ok and len(rows) == n and rows[0]["name"] == "Phone A"
          and float(rows[0]["latitude"]) == plugin.tracks[1][0]["latitude"], msg)
    gp = os.path.join(TMP, "phone_a.gpkg")
    ok, msg = plugin.export_device(1, gp)
    p_l = QgsVectorLayer(gp + "|layername=traccar_points", "p", "ogr")
    t_l = QgsVectorLayer(gp + "|layername=traccar_tracks", "t", "ogr")
    check("GeoPackage: every fix + one track, shared schema", ok and p_l.featureCount() == n
          and t_l.featureCount() == 1 and next(t_l.getFeatures())["n_points"] == n, msg)
    ok, msg = plugin.export_device(3, os.path.join(TMP, "spare.gpx"))
    check("device with no fixes → clear message, no file", not ok and "No fixes" in msg
          and not os.path.exists(os.path.join(TMP, "spare.gpx")), msg)


@test
def t16d_location_formats_and_copy():
    check("Irish Grid reference", mod.irish_grid_ref(315904, 234671) == "O 15904 34671",
          mod.irish_grid_ref(315904, 234671))
    check("UK Grid references", mod.uk_grid_ref(245140, 672887) == "NS 45140 72887"
          and mod.uk_grid_ref(651409, 313177) == "TG 51409 13177" and mod.uk_grid_ref(-5, 10) == "",
          (mod.uk_grid_ref(245140, 672887), mod.uk_grid_ref(651409, 313177)))
    pos = plugin.marker_pos[1]
    QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:2157"))
    plugin.cfg["custom_crs"] = "EPSG:29903"
    f = {k: plugin.format_location(pos, k) for k, _l in mod.COORD_FORMATS}
    import re
    check("Lat / Long: 'lat, lon' to 6 decimals", f["latlon"] == "%.6f, %.6f" % (pos["latitude"], pos["longitude"]),
          f["latlon"])
    check("Irish Grid: 'O 12345 67890' (Dublin)", re.fullmatch(r"O \d{5} \d{5}", f["ig"]) is not None, f["ig"])
    check("UK Grid: 'XX 12345 67890'", re.fullmatch(r"[A-Z]{2} \d{5} \d{5}", f["uk"]) is not None, f["uk"])
    x, y = (float(v) for v in f["project"].split(","))
    check("Project CRS (ITM): metres", 700000 < x < 730000 and 730000 < y < 750000, f["project"])
    check("Custom CRS (Irish Grid 29903) and its label",
          f["custom"] and plugin.coord_label("custom") == "Custom CRS (EPSG:29903)", (f["custom"], plugin.coord_label("custom")))
    plugin.set_coord_format("ig")
    d = plugin.dock
    r = [i for i, row in enumerate(d._rows) if row["name"] == "Phone A"][0]
    check("Location column shows the chosen format", d.table.item(r, 1).text() == f["ig"], d.table.item(r, 1).text())
    check("marker tooltip includes the location", f["ig"] in plugin._marker_tip(1, pos))
    cb = QgsApplication.clipboard()
    plugin.copy_text(plugin.location_wkt(pos), "WKT")
    pt = mod.QgsGeometry.fromWkt(cb.text()).asPoint()
    check("Copy WKT point (WGS84)", cb.text().startswith("Point (") and abs(pt.x() - pos["longitude"]) < 1e-7
          and abs(pt.y() - pos["latitude"]) < 1e-7, cb.text())
    n = len(plugin.tracks[1])
    plugin.copy_text(plugin.track_wkt(1, QgsProject.instance().crs()), "WKT track")
    g = mod.QgsGeometry.fromWkt(cb.text())
    check("Copy track as WKT LineString (project CRS) with every fix",
          cb.text().startswith("LineString") and len(list(g.vertices())) == n
          and next(g.vertices()).x() > 700000, cb.text()[:60])
    lines = plugin.track_lines(1).splitlines()
    check("Copy track as one location per line", len(lines) == n
          and lines[-1].endswith("\t" + plugin.format_location(plugin.tracks[1][-1], "ig")), lines[-1:])
    from qgis.PyQt.QtWidgets import QMenu
    seen = []
    orig_exec = QMenu.exec
    QMenu.exec = lambda self, *a: seen.extend(
        a.text() for m in [self] + [x.menu() for x in self.actions() if x.menu()] for a in m.actions())
    try:
        d._device_menu(r)
    finally:
        QMenu.exec = orig_exec
    check("⋯ menu: Copy location (with previews) and Copy track",
          any(t.startswith("Irish Grid:  O ") for t in seen) and any("WKT LineString" in t for t in seen)
          and any("what3words" in t for t in seen), seen)
    plugin.set_coord_format("latlon")


@test
def t16d2_latlon_styles():
    f = mod.format_latlon
    check("decimal degrees", f(53.365, -6.31, "dd") == "53.365000, -6.310000", f(53.365, -6.31, "dd"))
    L = "letters"
    check("degrees, minutes (N/S/E/W)", f(53.365, -6.31, "dm", L) == "53° 21.900' N, 6° 18.600' W",
          f(53.365, -6.31, "dm", L))
    check("degrees, minutes, seconds (N/S/E/W)",
          f(53.365, -6.31, "dms", L) == '53° 21\' 54.00" N, 6° 18\' 36.00" W', f(53.365, -6.31, "dms", L))
    check("south / east hemispheres", f(-33.5, 151.25, "dms", L) == '33° 30\' 00.00" S, 151° 15\' 00.00" E',
          f(-33.5, 151.25, "dms", L))
    check("decimal degrees with N/S/E/W", f(53.365, -6.31, "dd", L) == "53.365000 N, 6.310000 W",
          f(53.365, -6.31, "dd", L))
    check("+ / − signs with minutes and seconds",
          f(53.365, -6.31, "dm") == "53° 21.900', -6° 18.600'"
          and f(-33.5, 151.25, "dms") == '-33° 30\' 00.00", 151° 15\' 00.00"', (f(53.365, -6.31, "dm"), f(-33.5, 151.25, "dms")))
    check("no '-0' when a tiny negative rounds to zero", f(-0.0000001, 0) == "0.000000, 0.000000", f(-0.0000001, 0))
    check("rounding carries over (never 60.000')", f(53.9999999, 0, "dm", L).startswith("54° 00.000' N")
          and f(53.9999999, 0, "dms", L).startswith('54° 00\' 00.00" N'),
          (f(53.9999999, 0, "dm", L), f(53.9999999, 0, "dms", L)))
    pos = plugin.marker_pos[1]
    plugin.cfg.update(latlon_style="dms", latlon_hemi="letters")
    check("Lat / Long location uses the chosen style and hemisphere", plugin.format_location(pos, "latlon")
          == f(pos["latitude"], pos["longitude"], "dms", L) and plugin.format_location(pos, "latlon").endswith(" W"),
          plugin.format_location(pos, "latlon"))
    plugin.cfg["custom_crs"] = "EPSG:4258"                     # a geographic custom CRS (ETRS89)
    check("geographic custom CRS uses it too", plugin.format_location(pos, "custom").count('"') == 2,
          plugin.format_location(pos, "custom"))
    sd = mod.SettingsDialog(plugin, IFACE.mainWindow(), 3)
    check("Settings: Lat / Long style and hemisphere round-trip",
          sd.values()["latlon_style"] == "dms" and sd.values()["latlon_hemi"] == "letters")
    sd.deleteLater()
    flush_deletes()
    plugin.cfg.update(latlon_style="dd", latlon_hemi="signs", custom_crs="EPSG:29903")


@test
def t16e_what3words():
    mod.W3W_URL = URL + "/v3"
    pos = plugin.marker_pos[1]
    cb = QgsApplication.clipboard()
    cb.setText("")
    plugin.set_w3w_key("wrong-key")
    plugin.copy_w3w(pos)
    wait(lambda: plugin._w3w_reply is None, 5)
    check("what3words: wrong key → nothing copied", cb.text() == "" and len(W3W_CALLS) == 1)
    plugin.set_w3w_key(W3W_KEY)
    plugin._w3w = None                      # read it back from the password manager
    check("what3words key kept in QGIS's password manager", plugin.w3w_key() == W3W_KEY)
    plugin.copy_w3w(pos)
    wait(lambda: cb.text() != "", 5)
    check("what3words address copied", cb.text() == "///filled.count.soap", cb.text())
    check("key sent in a header, not the URL", W3W_KEY not in W3W_CALLS[-1], W3W_CALLS[-1])
    QSettings().sync()
    import glob
    leaked = [f for f in glob.glob(os.path.join(TMP, "**", "*.ini"), recursive=True)
              if W3W_KEY in open(f, encoding="utf-8", errors="ignore").read()]
    check("what3words key not in any settings file", not leaked, leaked)
    s = mod.SettingsDialog(plugin, IFACE.mainWindow(), 3)
    v = s.values()
    check("Settings: custom CRS round-trips, key box blank ('saved')",
          v["custom_crs"] == plugin.cfg["custom_crs"] and v["w3w_key"] == "" and "saved" in s.w3w_edit.placeholderText(),
          (v["custom_crs"], s.w3w_edit.placeholderText()))
    s.deleteLater()
    flush_deletes()


@test
def t16f_playback():
    plugin.set_live_view("canvas")
    load(180)
    plugin.open_main()
    d = plugin.dock
    tr = plugin.tracks[1]
    rng = plugin.play_range()
    check("playback range = first … last fix of the shown tracks", rng == (tr[0]["_t"], tr[-1]["_t"]), rng)
    mid = tr[10]["_t"] + timedelta(seconds=15)               # half-way between fixes 10 and 11 (30 s apart)
    plugin.set_play_time(mid)
    trk, mk = plugin._overlay_data()
    exp_lon = (tr[10]["longitude"] + tr[11]["longitude"]) / 2
    exp_lat = (tr[10]["latitude"] + tr[11]["latitude"]) / 2
    check("marker interpolated half-way between two fixes",
          abs(mk[1]["longitude"] - exp_lon) < 1e-9 and abs(mk[1]["latitude"] - exp_lat) < 1e-9,
          (mk[1]["longitude"], exp_lon))
    item = plugin._item
    check("track drawn up to the playback time, whole track faint underneath",
          len(trk[1]) == 12 and len(item.tracks) == 2 and item.tracks[0][0].alpha() == 70
          and len(item.tracks[1][1]) == 12, (len(trk[1]), len(item.tracks)))
    check("only devices with fixes so far get a marker", list(mk) == [1] and len(item.markers) == 1)
    check("marker tooltip shows the playback time", "Playback" in item.markers[0]["tip"], item.markers[0]["tip"])
    check("slider + label follow the playback time",
          d.pb_slider.value() == int((mid - rng[0]).total_seconds()) and "paused" in d.pb_lbl.text(),
          (d.pb_slider.value(), d.pb_lbl.text()))
    d.pb_slider.setValue(0)
    check("dragging the slider moves playback", plugin.play_t == rng[0], plugin.play_t)
    plugin.cfg["play_speed"] = 600
    plugin.play(True)
    wait(lambda: False, 0.6)
    adv = (plugin.play_t - rng[0]).total_seconds()
    check("▶ plays at the chosen speed (600 s of track per second)", 150 < adv < 600 and d.pb_play.text() == "⏸", adv)
    plugin.cfg["play_speed"] = 86400
    wait(lambda: not plugin.playing, 3)
    check("pauses at the end of the tracks", not plugin.playing and plugin.play_t == rng[1]
          and d.pb_play.text() == "▶" and not plugin.play_timer.isActive())
    plugin.set_live_view("layers")
    plugin.set_play_time(mid)
    mkl = QgsProject.instance().mapLayer(plugin._mk_id)
    tkl = QgsProject.instance().mapLayer(plugin._tk_id)
    g = next(mkl.getFeatures()).geometry().asPoint()
    check("temporary layers follow playback too", mkl.featureCount() == 1 and abs(g.x() - exp_lon) < 1e-9
          and len(list(next(tkl.getFeatures()).geometry().vertices())) == 12)
    plugin.stop_playback()
    check("■ leaves playback: latest positions again", plugin.play_t is None and mkl.featureCount() == 2
          and not d.pb_stop.isEnabled())
    plugin.set_live_view("canvas")
    plugin.cfg["play_speed"] = 60


@test
def t16g_playback_goto_pan_copy():
    load(180)
    plugin.open_main()
    d = plugin.dock
    tr = plugin.tracks[1]
    target = tr[20]
    d.pb_time.setDateTime(mod._qdt_local(target["_t"]))        # as if typed / picked in the "At:" box
    check("typing a time in 'At:' moves playback there", plugin.play_t == target["_t"], (plugin.play_t, target["_t"]))
    pos = plugin.shown_pos(1)
    check("shown position = the fix at that time", abs(pos["longitude"] - target["longitude"]) < 1e-9
          and abs(pos["latitude"] - target["latitude"]) < 1e-9)
    r = [i for i, row in enumerate(d._rows) if row["name"] == "Phone A"][0]
    check("Location column follows playback", d.table.item(r, 1).text() == plugin.format_location(target),
          d.table.item(r, 1).text())
    tr[20]["speed"], tr[21]["speed"] = 20.0, 30.0                  # knots
    tr[20]["attributes"] = dict(tr[20]["attributes"], batteryLevel=55)
    plugin.set_play_time(tr[20]["_t"])
    check("Speed / Battery columns follow playback (at a fix)",
          d.table.item(r, 4).text() == "37 km/h" and d.table.item(r, 5).text() == "55%",
          (d.table.item(r, 4).text(), d.table.item(r, 5).text()))
    plugin.set_play_time(tr[20]["_t"] + timedelta(seconds=15))      # half-way: 25 kn
    check("Speed interpolated between fixes", d.table.item(r, 4).text() == "46 km/h", d.table.item(r, 4).text())
    tr[21]["accuracy"] = 25.0                                      # tr[20] is 6 m
    pos = plugin.shown_pos(1)
    check("accuracy between fixes = the larger of the two ('somewhere in here')", pos["accuracy"] == 25.0,
          pos["accuracy"])
    plugin.set_live_view("layers")
    mkl = QgsProject.instance().mapLayer(plugin._mk_id)
    check("temporary layers: accuracy_m during playback", next(mkl.getFeatures())["accuracy_m"] == 25.0)
    plugin.set_live_view("canvas")
    plugin.set_play_time(tr[20]["_t"])
    check("on an exact fix: that fix's own accuracy", plugin.shown_pos(1)["accuracy"] == 6.0,
          plugin.shown_pos(1)["accuracy"])
    plugin.set_play_time(target["_t"])

    canvas = IFACE.mapCanvas()
    xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), canvas.mapSettings().destinationCrs(),
                                QgsProject.instance())
    exp = xf.transform(mod.QgsPointXY(target["longitude"], target["latitude"]))
    canvas.setCenter(mod.QgsPointXY(exp.x() + 5000, exp.y() + 5000))
    d.pb_pan.click()                                          # no device selected: the shown devices (only Phone A)
    c = canvas.center()
    check("⌖ pans to the position shown at the playback time",
          abs(c.x() - exp.x()) < 1 and abs(c.y() - exp.y()) < 1, (c.x(), exp.x()))
    plugin.pan_to_shown(3)                                    # Spare: never on the map
    check("pan to a device not on the map at that time: message, no move", abs(canvas.center().x() - c.x()) < 1e-6)

    from qgis.PyQt.QtWidgets import QMenu
    cb = QgsApplication.clipboard()
    cb.setText("")
    seen = []

    def fake_exec(menu, *a):
        for act in menu.actions():
            sub = act.menu()
            for sa in (sub.actions() if sub else []):
                seen.append(sa.text())
                if sa.text().startswith("Lat / Long:"):
                    sa.trigger()
            seen.append(act.text())
    orig_exec = QMenu.exec
    QMenu.exec = fake_exec
    try:
        d._device_menu(r)
    finally:
        QMenu.exec = orig_exec
    check("⋯ menu: 'Copy location (playback time)' and 'Pan to device'",
          any("playback time" in t for t in seen) and "Pan to device" in seen, seen)
    check("copied location = position at the playback time",
          cb.text() == "%.6f, %.6f" % (target["latitude"], target["longitude"]), cb.text())
    plugin.stop_playback()
    check("after playback, Location / Speed / Battery back to the latest fix",
          d.table.item(r, 1).text() == plugin.format_location(plugin.marker_pos[1])
          and d.table.item(r, 4).text() == "5 km/h" and d.table.item(r, 5).text() == "81%",
          (d.table.item(r, 1).text(), d.table.item(r, 4).text()))


@test
def t16h_playback_long_track_keeps_its_shape():
    plugin.set_live_view("canvas")
    load(180)
    base = dict(plugin.tracks[1][0])
    t0 = base["_t"]
    zigzag = [dict(base, longitude=-6.33 + 0.0001 * i, latitude=53.35 + 0.001 * (i % 2),
                   _t=t0 + timedelta(seconds=i)) for i in range(5000)]   # > the old 2000-point limit
    plugin.tracks = {1: zigzag}
    item = plugin._item

    def poly_pts(poly):
        return [(round(poly[k].x(), 6), round(poly[k].y(), 6)) for k in range(len(poly))]
    plugin.set_play_time(t0 + timedelta(seconds=3000.5))
    whole, played = poly_pts(item.tracks[0][1]), poly_pts(item.tracks[1][1])
    check("long track drawn with every vertex (no thinning)", len(whole) == 5000, len(whole))
    check("played part = the full track's own vertices + the moving end",
          len(played) == 3002 and played[:3001] == whole[:3001], len(played))
    plugin.set_play_time(t0 + timedelta(seconds=4200.5))
    later = poly_pts(item.tracks[1][1])
    check("playing on never changes the route already drawn", later[:3001] == played[:3001] and len(later) == 4202,
          (len(later), [k for k in range(3001) if later[k] != played[k]][:3], later[3000:3002], played[3000:3002]))
    plugin.stop_playback()
    load(180)


@test
def t17_clear_and_stale():
    plugin.clear()
    prj = QgsProject.instance()
    check("Clear removes overlay layers and group",
          not plugin._mk_id and prj.layerTreeRoot().findGroup(mod.GROUP_NAME) is None)
    plugin.set_live_view("canvas")
    load(60)
    item = plugin._item
    plugin.clear()
    check("Clear removes the drawn-on-map view",
          plugin._item is None and item not in IFACE.mapCanvas().scene().items())
    stale = add(QgsVectorLayer("Point?crs=EPSG:4326", "Traccar markers", "memory"))
    stale.setCustomProperty(mod.OVERLAY_PROP, 1)
    sid = stale.id()
    plugin._remove_stale_overlays()
    check("stale overlay from a saved project is dropped", prj.mapLayer(sid) is None)


@test
def t18_migration_from_v01():
    s = QSettings()
    for k in list(mod.DEFAULTS):
        s.remove("TraccarLive/" + k)
    pts = QgsProject.instance().mapLayer(plugin.cfg["points_layer_id"])
    s.setValue("TraccarLive/append_pts", True)
    s.setValue("TraccarLive/pt_layer_id", pts.id())
    s.setValue("TraccarLive/append_lines", False)
    s.setValue("TraccarLive/ln_layer_id", "something")
    s.setValue("TraccarLive/interval_min", 3)
    p2 = mod.TraccarLive(IFACE)
    check("v0.1 settings migrated (B → positions, C off, 3 min → 180 s)",
          p2.cfg["points_layer_id"] == pts.id() and p2.cfg["tracks_layer_id"] == ""
          and p2.cfg["live_interval_s"] == 180, (p2.cfg["points_layer_id"], p2.cfg["tracks_layer_id"],
                                                 p2.cfg["live_interval_s"]))


@test
def t18b_password_store():
    import glob
    from qgis.core import QgsAuthMethodConfig
    login = mod.load_login(plugin.cfg["authcfg"])
    check("login kept in QGIS's password manager", login == (USER, PWD), login)
    c = QgsAuthMethodConfig()
    res = QgsApplication.authManager().loadAuthenticationConfig(plugin.cfg["authcfg"], c, True)
    c = res[1] if isinstance(res, tuple) else c
    check("stored as a QGIS 'Basic' login named Traccar Live", c.method() == "Basic" and c.name() == "Traccar Live",
          (c.method(), c.name()))
    plugin.set_login(URL, USER, "")
    check("blank password keeps the saved one", mod.load_login(plugin.cfg["authcfg"]) == (USER, PWD))
    s = QSettings()
    s.setValue("TraccarLive/authcfg", "")
    s.setValue("TraccarLive/password", PWD)                 # as left by v0.2.0
    p3 = mod.TraccarLive(IFACE)
    got = p3._credentials()
    check("old plain-text password moved into the password manager",
          got == (USER, PWD) and p3.cfg["authcfg"] and mod.load_login(p3.cfg["authcfg"]) == (USER, PWD)
          and QSettings().value("TraccarLive/password", None) is None, (got, p3.cfg["authcfg"]))
    d = mod.SettingsDialog(p3, IFACE.mainWindow(), 0)
    check("Settings shows an empty password box ('saved — leave blank')",
          d.pass_edit.text() == "" and "saved" in d.pass_edit.placeholderText())
    d.deleteLater()
    flush_deletes()
    p3._save_settings()
    QSettings().sync()
    leaked = [f for f in glob.glob(os.path.join(TMP, "**", "*.ini"), recursive=True)
              if PWD in open(f, encoding="utf-8", errors="ignore").read()]
    check("no password in any settings file on disk", not leaked, leaked)


@test
def t19_unload():
    plugin.unload()
    check("unload cleanly", plugin.dock is None and not plugin._actions)


# ── Report ───────────────────────────────────────────────────────────────────
server.shutdown()
print("\nQGIS %s | Qt %s | Python %s" % (Qgis.version(),
      __import__("qgis.PyQt.QtCore", fromlist=["QT_VERSION_STR"]).QT_VERSION_STR, sys.version.split()[0]))
passed = sum(1 for _n, ok, _d in RESULTS if ok)
for name, ok, detail in RESULTS:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or detail == "" else "   → %s" % (detail,)))
print("%d / %d passed" % (passed, len(RESULTS)))
QgsProject.instance().clear()
shutil.rmtree(TMP, ignore_errors=True)
sys.stdout.flush()
sys.stderr.flush()
from qgis.core import QgsApplication  # noqa: E402
QgsApplication.exitQgis()              # shut QGIS down cleanly before leaving
os._exit(0 if passed == len(RESULTS) else 1)
