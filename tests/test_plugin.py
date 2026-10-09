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

from qgis.PyQt.QtCore import QSettings, QCoreApplication, QDateTime  # noqa: E402
QSettings.setDefaultFormat(QSettings.Format.IniFormat if hasattr(QSettings, "Format") else QSettings.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat if hasattr(QSettings, "Format") else QSettings.IniFormat,
                  QSettings.Scope.UserScope if hasattr(QSettings, "Scope") else QSettings.UserScope, TMP)

from qgis.testing import start_app            # noqa: E402
from qgis.testing.mocked import get_iface     # noqa: E402
from qgis.core import (                       # noqa: E402
    Qgis, QgsProject, QgsVectorLayer, QgsVectorFileWriter, QgsWkbTypes, QgsFeature,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PKG = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fake Traccar server ──────────────────────────────────────────────────────
NOW  = datetime.now(timezone.utc).replace(microsecond=0)
USER, PWD = "test@example.com", "secret"
DEVICES = [{"id": 1, "name": "Phone A", "status": "online"},
           {"id": 2, "name": "Van 3", "status": "offline"},
           {"id": 3, "name": "Spare", "status": "unknown"}]


def _fix(dev, t, lon, lat, i):
    return {"id": dev * 100000 + i, "deviceId": dev, "fixTime": t.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            "latitude": lat, "longitude": lon, "altitude": 90.0 + i % 5, "speed": 2.5,
            "course": 45.0, "accuracy": 6.0, "address": None,
            "attributes": {"batteryLevel": 81, "motion": True}}


POS = {1: [], 2: [], 3: []}
for i in range(240):                                   # last 2 h, every 30 s
    t = NOW - timedelta(seconds=30 * (239 - i))
    POS[1].append(_fix(1, t, -9.50 + i * 0.0001, 52.05 + i * 0.00005, i))
VAN_START = NOW - timedelta(days=2)
for i in range(20):                                    # two days ago, 10 minutes
    POS[2].append(_fix(2, VAN_START + timedelta(seconds=30 * i), -9.40 + i * 0.0002, 52.10, i))


def _p(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        import base64
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
plugin.cfg.update(server_url=URL, username=USER, password="wrong")


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
    plugin.cfg["password"] = PWD
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
    loc = lambda d: d.astimezone().strftime("%Y-%m-%d %H:%M")
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
    new = _fix(1, datetime.now(timezone.utc) + timedelta(seconds=1), -9.47, 52.06, 999)
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
    d = plugin.dlg
    wait(lambda: False, 0.2)
    check("main window opens (non-modal)", d.isVisible() and not d.isModal())
    check("device table filled", d.table.rowCount() == 3, d.table.rowCount())
    check("saves-to line shows layers", "Positions →" in d.saves_to_lbl.text())
    plugin.set_live(True)
    check("Live button and toolbar action in sync", d.live_btn.isChecked() and plugin.act_live.isChecked()
          and plugin.timer.isActive())
    d.close()
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
def t19_unload():
    plugin.unload()
    check("unload cleanly", plugin.dlg is None and not plugin._actions)


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
