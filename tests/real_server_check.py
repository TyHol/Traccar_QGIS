"""
Read-only check of Traccar Live against a real Traccar server.

Uses the server and username saved by the plugin in a QGIS profile, and the password from
the TRACCAR_PASSWORD environment variable (or, until the plugin has moved it into QGIS's
password manager, the old plain-text one in that profile). Nothing is printed. Only makes
GET requests (devices, positions), and saves into a throwaway GeoPackage that is
deleted afterwards.

    set TRACCAR_QGIS_INI=C:\\Users\\<you>\\AppData\\Roaming\\QGIS\\QGIS3\\profiles\\<profile>\\QGIS\\QGIS3.ini
    set QT_QPA_PLATFORM=offscreen
    python-qgis.bat tests\\real_server_check.py [window minutes, default 1440]
"""

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = tempfile.mkdtemp(prefix="traccar_real_")

from qgis.PyQt.QtCore import QSettings, QCoreApplication  # noqa: E402
_INI = QSettings.Format.IniFormat if hasattr(QSettings, "Format") else QSettings.IniFormat
_USER = QSettings.Scope.UserScope if hasattr(QSettings, "Scope") else QSettings.UserScope
QSettings.setDefaultFormat(_INI)
QSettings.setPath(_INI, _USER, TMP)          # the test never touches real QGIS settings

from qgis.testing import start_app            # noqa: E402
from qgis.testing.mocked import get_iface     # noqa: E402
from qgis.core import Qgis, QgsProject        # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PKG = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

src = QSettings(os.environ["TRACCAR_QGIS_INI"], _INI)
creds = {k: src.value("TraccarLive/" + k, "") for k in ("server_url", "username", "password")}
creds["password"] = os.environ.get("TRACCAR_PASSWORD") or creds["password"]
if not creds["username"] or not creds["password"]:
    sys.exit("No Traccar login: set TRACCAR_PASSWORD (the profile has no plain-text password).")
minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 1440

start_app()
from qgis.core import QgsApplication  # noqa: E402
QgsApplication.authManager().setMasterPassword("check-master", True)   # temporary auth DB only
mod = __import__(PKG + ".traccar_live", fromlist=["*"])
plugin = mod.TraccarLive(get_iface())
plugin.set_login(creds["server_url"], creds["username"], creds["password"])
plugin.cfg["window_minutes"] = minutes


def wait(cond, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        QCoreApplication.processEvents()
        time.sleep(0.01)
    return cond()


print("QGIS %s — %s as %s — window: last %d min" % (
    Qgis.version(), creds["server_url"], creds["username"], minutes))
t0 = time.time()
plugin.reload_window()
wait(lambda: not plugin.loading)
print("Loaded in %.1f s  %s" % (time.time() - t0, ("⚠ " + plugin.last_error) if plugin.last_error else ""))
if plugin.win is None:
    sys.exit("Nothing loaded.")
print("Window:", plugin.window_summary().replace("\n", " "))
print("\n%-24s %6s  %-20s %-9s %-8s %s" % ("Device", "Fixes", "Time span (local)", "Speed", "Battery", "Last fix"))
for r in plugin.device_rows:
    print("%-24s %6s  %-20s %-9s %-8s %s" % (r["name"][:24], r["fixes"], r["span"], r["speed"],
                                             r["battery"], r["tip"]))

# Save into a throwaway GeoPackage (both modes) and read back
pts, trk, err = mod.create_template_gpkg(os.path.join(TMP, "check.gpkg"))
QgsProject.instance().addMapLayer(pts)
QgsProject.instance().addMapLayer(trk)
plugin.cfg.update(points_layer_id=pts.id(), tracks_layer_id=trk.id(), points_mode=0)
plugin.save_positions()
n_latest = pts.featureCount()
plugin.cfg["points_mode"] = 1
plugin.save_positions()
plugin.save_tracks()
print("\nSaved: %d latest point(s), %d point(s) for every fix, %d track(s)"
      % (n_latest, pts.featureCount() - n_latest, trk.featureCount()))
for f in trk.getFeatures():
    g = f.geometry()
    print("  track %-20s %5d vertices  start %s (stored UTC: %s)" % (
        f["name"], len(list(g.vertices())), f["start_local"],
        f["start_time"].toString("yyyy-MM-dd HH:mm:ss t")))
if plugin._item is not None:
    print("Live view (drawn on map): %d marker(s), %d track(s)"
          % (len(plugin._item.markers), len(plugin._item.tracks)))
else:
    mk = QgsProject.instance().mapLayer(plugin._mk_id)
    print("Live view (Temp layers): %d marker(s)" % (mk.featureCount() if mk else 0))
plugin.clear()
QgsProject.instance().clear()
shutil.rmtree(TMP, ignore_errors=True)
sys.stdout.flush()
from qgis.core import QgsApplication  # noqa: E402
QgsApplication.exitQgis()
os._exit(0)
