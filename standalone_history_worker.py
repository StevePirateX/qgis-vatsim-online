"""Standalone VATSIM history recorder: records to daily history files without opening QGIS.

It shares its code with the plugin, so the two always agree on:
  * the daily file names and where they go                  (history_files.py)
  * the table layout and how a GeoPackage is created        (history_table.py)
  * which aircraft get a new row - a stationary aircraft gets one row, whose last_seen time
    is pushed forward while nothing changes                 (stationary_filter.py)
  * how a snapshot is written, the switch to a new file when
    the day changes, and converting the finished day to
    GeoParquet                                              (history_recorder.py,
                                                             geoparquet_handler.py)
Only the loop, the settings and the VATSIM download live here.

One GeoPackage is written per day: <folder>/<base name>-YYYY-MM-DD.gpkg. When the day is over it
is converted to GeoParquet (<base name>-YYYY-MM-DD.parquet) on a background thread, and the
GeoPackage is removed once the conversion has been checked. Older days left unconverted (for
example because this wasn't running at midnight) are converted when it starts.

Settings come from the plugin's .env file:
    VATSIM_DATA_URL
    GPKG_PATH              where to record. A file such as /data/vatsim-history.gpkg means the
                           folder /data and the base name "vatsim-history"; a folder uses the
                           default base name. (Same meaning as the plugin's history file box.)
    TABLE_NAME
    RECORD_INTERVAL_SECS   (optional, default 15)
    CONVERT_TO_GEOPARQUET  (optional, default 1; 0 keeps the daily GeoPackages as they are)

Run it with a Python that has GDAL (osgeo), requests, python-dotenv and geoparquet-io.
Ctrl+C stops it cleanly (press it again to quit immediately).
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

from vatsim_plugin.vatsim_data import VatsimData                 # noqa: E402
from history_files import resolve_base                           # noqa: E402
from history_recorder import DailyHistory, configure_sqlite      # noqa: E402

# --- Settings
load_dotenv(plugin_root / ".env")
VATSIM_DATA_URL = os.getenv("VATSIM_DATA_URL")
GPKG_PATH = os.getenv("GPKG_PATH")
TABLE_NAME = os.getenv("TABLE_NAME")
INTERVAL_SECS = float(os.getenv("RECORD_INTERVAL_SECS", 15))
CONVERT_TO_GEOPARQUET = os.getenv("CONVERT_TO_GEOPARQUET", "1") != "0"

if not GPKG_PATH or not TABLE_NAME:
    raise ValueError("GPKG_PATH and TABLE_NAME must be set in .env")

# --- Ctrl+C finishes the current cycle, then closes the file properly
stop_requested = False


def request_stop(signum, frame):
    global stop_requested
    if stop_requested:
        print("\nQuitting immediately.")
        sys.exit(1)
    stop_requested = True
    print("\nStopping after the current cycle (press Ctrl+C again to quit immediately)...")


signal.signal(signal.SIGINT, request_stop)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, request_stop)


def log(message: str, critical: bool = False):
    """Progress and errors from the history code (including the conversion thread)."""
    print(f"\n{'ERROR: ' if critical else ''}{message}", file=sys.stderr if critical else sys.stdout)


def main():
    configure_sqlite(wal=True, synchronous="NORMAL")
    vatsim_data = VatsimData(VATSIM_DATA_URL) if VATSIM_DATA_URL else VatsimData()

    folder, base_name = resolve_base(GPKG_PATH, default_folder=os.getcwd())
    history = DailyHistory(folder, base_name, TABLE_NAME, convert=CONVERT_TO_GEOPARQUET, log=log)
    last_update = None

    print(f"Recording VATSIM data to daily files in {folder} ({base_name}-YYYY-MM-DD.gpkg, table "
          f"'{TABLE_NAME}'), every {INTERVAL_SECS:g} seconds.")
    print("Finished days are converted to GeoParquet." if CONVERT_TO_GEOPARQUET
          else "Finished days are kept as GeoPackages (CONVERT_TO_GEOPARQUET=0).")
    print("Press Ctrl+C to stop.")

    while not stop_requested:
        cycle_start = time.time()
        print(f"{datetime.now():%H:%M:%S} - ", end="")
        try:
            vatsim_data.fetch_vatsim_data()
            if vatsim_data.timestamp == last_update:
                print("no new VATSIM data yet. ", end="")   # the feed only changes about every 15 s
            else:
                pilots = vatsim_data.pilots
                # opens today's file (creating it, and switching files when the day changes)
                stats = history.write(pilots, vatsim_data.get_update_time())
                last_update = vatsim_data.timestamp
                print(f"Data {vatsim_data.timestamp:%H:%M:%S}  |  {len(pilots)} pilots  |  "
                      f"{stats['written']} new rows, {stats['extended']} unchanged extended  |  "
                      f"Runtime: {time.time() - cycle_start:.2f} secs. ", end="")
        except Exception as e:
            # Log the error and carry on - don't crash the loop. The file is reopened on the
            # next write in case it went away (e.g. a drive was unmounted).
            print(f"Error during cycle: {e}", file=sys.stderr)
            history.reset()

        sleep_time = max(0.0, INTERVAL_SECS - (time.time() - cycle_start))
        print(f"Sleeping for {sleep_time:.2f} secs")
        wake_at = time.time() + sleep_time
        while not stop_requested and time.time() < wake_at:
            time.sleep(min(0.25, max(0.0, wake_at - time.time())))

    history.close(wait_for_conversion=True)  # closes the file; lets a running conversion finish
    print("Stopped.")


if __name__ == "__main__":
    main()