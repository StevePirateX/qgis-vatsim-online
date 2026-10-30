"""Standalone VATSIM history recorder: records to the history GeoPackage without opening QGIS.

It shares its code with the plugin, so the two always agree on:
  * the table layout and how the GeoPackage is created      (history_table.py)
  * which aircraft get a new row - a stationary aircraft gets one row, whose last_seen time
    is pushed forward while nothing changes                 (stationary_filter.py)
  * how a snapshot is written                               (history_recorder.py)
Only the loop, the settings and the VATSIM download live here.

Settings come from the plugin's .env file:
    VATSIM_DATA_URL, GPKG_PATH, TABLE_NAME
    RECORD_INTERVAL_SECS   (optional, default 15)

Run it with a Python that has GDAL (osgeo), requests and python-dotenv. Ctrl+C stops it cleanly.
"""
import os
import signal
import sys
import time
import types
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from osgeo import gdal

gdal.UseExceptions()

# --- Find the plugin folder: this script can sit in it or in any folder below it
script = Path(__file__).resolve()
plugin_root = script.parent
while not (plugin_root / "vatsim_data.py").exists():
    if plugin_root.parent == plugin_root:
        raise RuntimeError("Cannot locate plugin root - vatsim_data.py not found")
    plugin_root = plugin_root.parent

sys.path.insert(0, str(plugin_root))  # the shared history modules use plain imports

# vatsim_data.py uses relative imports, so it has to be imported as part of a package
package = types.ModuleType("vatsim_plugin")
package.__path__ = [str(plugin_root)]
package.__file__ = str(plugin_root / "__init__.py")
sys.modules["vatsim_plugin"] = package

from vatsim_plugin.vatsim_data import VatsimData                # noqa: E402
from history_table import open_history_layer                    # noqa: E402
from history_recorder import write_snapshot, configure_sqlite   # noqa: E402
from stationary_filter import StationaryFilter                  # noqa: E402

# --- Settings
load_dotenv(plugin_root / ".env")
VATSIM_DATA_URL = os.getenv("VATSIM_DATA_URL")
GPKG_PATH = os.getenv("GPKG_PATH")
TABLE_NAME = os.getenv("TABLE_NAME")
INTERVAL_SECS = float(os.getenv("RECORD_INTERVAL_SECS", 15))

if not GPKG_PATH or not TABLE_NAME:
    raise ValueError("GPKG_PATH and TABLE_NAME must be set in .env")

# --- Ctrl+C finishes the current cycle, then closes the file properly
stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    stop_requested = True
    print("\nStopping after the current cycle...")


signal.signal(signal.SIGINT, request_stop)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, request_stop)


def main():
    configure_sqlite(wal=True, synchronous="NORMAL")
    vatsim_data = VatsimData(VATSIM_DATA_URL) if VATSIM_DATA_URL else VatsimData()
    stationary_filter = StationaryFilter()   # same rules and defaults as the plugin

    ds = layer = defn = idx = None
    last_update = None

    print(f"Recording VATSIM data to {GPKG_PATH} (table '{TABLE_NAME}'), every {INTERVAL_SECS:g} seconds.")
    print("Press Ctrl+C to stop.")

    while not stop_requested:
        cycle_start = time.time()
        print(f"{datetime.now():%H:%M:%S} - ", end="")
        try:
            if layer is None:
                # creates the GeoPackage and table if they don't exist yet
                ds, layer = open_history_layer(GPKG_PATH, TABLE_NAME)
                defn = layer.GetLayerDefn()
                idx = {defn.GetFieldDefn(i).GetName(): i for i in range(defn.GetFieldCount())}

            vatsim_data.fetch_vatsim_data()
            if vatsim_data.timestamp == last_update:
                print("no new VATSIM data yet. ", end="")   # the feed only changes about every 15 s
            else:
                pilots = vatsim_data.pilots
                stats = write_snapshot(ds, layer, defn, idx, stationary_filter, pilots,
                                       vatsim_data.get_update_time())
                last_update = vatsim_data.timestamp
                print(f"Data {vatsim_data.timestamp:%H:%M:%S}  |  {len(pilots)} pilots  |  "
                      f"{stats['written']} new rows, {stats['extended']} unchanged extended  |  "
                      f"Runtime: {time.time() - cycle_start:.2f} secs. ", end="")
        except Exception as e:
            # Log the error and carry on - don't crash the loop. Reopen the file next cycle
            # in case it went away (e.g. a drive was unmounted).
            print(f"Error during cycle: {e}", file=sys.stderr)
            ds = layer = None

        sleep_time = max(0.0, INTERVAL_SECS - (time.time() - cycle_start))
        print(f"Sleeping for {sleep_time:.2f} secs")
        wake_at = time.time() + sleep_time
        while not stop_requested and time.time() < wake_at:
            time.sleep(min(0.25, max(0.0, wake_at - time.time())))

    layer = ds = None  # closing the dataset flushes everything to disk
    print("Stopped.")


if __name__ == "__main__":
    main()