from datetime import datetime, timezone
import sys
import os
from pathlib import Path
import time
import importlib

from osgeo import ogr, osr, gdal
gdal.UseExceptions()

script = Path(__file__).resolve()
plugin_root = script.parent
while not (plugin_root / "vatsim_data.py").exists():
    if plugin_root.parent == plugin_root:
        raise RuntimeError("Cannot locate plugin root – vatsim_data.py not found")
    plugin_root = plugin_root.parent

valid_pkg_name = "vatsim_plugin"
pkg = type(sys)(valid_pkg_name)
pkg.__path__ = [str(plugin_root)]
pkg.__file__ = str(plugin_root / "__init__.py")
sys.modules[valid_pkg_name] = pkg
from vatsim_plugin.vatsim_data import VatsimData    # noqa - No QA from IDE

from dotenv import load_dotenv
load_dotenv(plugin_root / '.env')
VATSIM_DATA_URL = os.getenv("VATSIM_DATA_URL")
GPKG_PATH = os.getenv("GPKG_PATH")
TABLE_NAME = os.getenv("TABLE_NAME")

if not GPKG_PATH or not TABLE_NAME:
    raise ValueError("GPKG_PATH and TABLE_NAME must be set in .env")

FIELDS = [
    ("callsign", ogr.OFTString, 8),
    ("cid", ogr.OFTInteger, 0),
    ("heading", ogr.OFTInteger, 0),
    ("groundspeed", ogr.OFTInteger, 0),
    ("departure", ogr.OFTString, 4),
    ("arrival", ogr.OFTString, 4),
    ("aircraft", ogr.OFTString, 8),
    ("timestamp", ogr.OFTDateTime, 0),
]


def _safe_create_spatial_index(ds, table_name, geom_col='geom'):
    try:
        ds.ExecuteSQL(f"SELECT CreateSpatialIndex('{table_name}', '{geom_col}')")
    except RuntimeError as e:
        if 'already existing' not in str(e):
            raise

def ensure_geopackage(gpkg_path, table_name, fields):
    file_exists = os.path.exists(gpkg_path)

    def _create_layer(ds):
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = ds.CreateLayer(table_name, srs, ogr.wkbPoint25D)
        for name, ogr_type, width in fields:
            field = ogr.FieldDefn(name, ogr_type)
            if width:
                field.SetWidth(width)
            layer.CreateField(field)
        return layer

    if not file_exists:
        driver = ogr.GetDriverByName("GPKG")
        ds = driver.CreateDataSource(gpkg_path)
        if ds is None:
            raise RuntimeError(f"Could not create GeoPackage at {gpkg_path}")
        _create_layer(ds)
        _safe_create_spatial_index(ds, table_name)
        ds = None
        print(f"Created GeoPackage and table '{table_name}'.")
    else:
        ds = ogr.Open(gpkg_path, update=0)
        layer = ds.GetLayerByName(table_name)
        if layer is None:
            ds = None
            ds = ogr.Open(gpkg_path, update=1)
            _create_layer(ds)
            _safe_create_spatial_index(ds, table_name)
            ds = None
            print(f"Created table '{table_name}' in existing GeoPackage.")
        else:
            ds = None

ensure_geopackage(GPKG_PATH, TABLE_NAME, FIELDS)

# 4. Run your logic
print("Starting VATSIM data recording...")
vatsim_data = VatsimData(VATSIM_DATA_URL)

def get_vatsim_data():
    vatsim_data.fetch_vatsim_data()
    pilots = vatsim_data.pilots
    return pilots

def save_to_gpkg(pilots):
    # Open the GeoPackage in update mode
    driver = ogr.GetDriverByName("GPKG")
    ds = driver.Open(GPKG_PATH, update=1)
    layer = ds.GetLayerByName(TABLE_NAME)
    layer_defn = layer.GetLayerDefn()
    field_indices = {layer_defn.GetFieldDefn(i).GetName(): i for i in range(layer_defn.GetFieldCount())}
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

    # --- START TRANSACTION ---
    layer.StartTransaction()

    try:
        for i, pilot in enumerate(pilots):
            feature = ogr.Feature(layer_defn)
            point = ogr.Geometry(ogr.wkbPoint25D)
            point.AddPoint(pilot.longitude, pilot.latitude, pilot.altitude)
            feature.SetGeometry(point)

            attr_map = {
                "callsign": pilot.callsign,
                "cid": pilot.cid,
                "heading": pilot.heading,
                "groundspeed": pilot.groundspeed,
                "departure": pilot.departure,
                "arrival": pilot.arrival,
                "aircraft": pilot.aircraft,
                "timestamp": vatsim_data.get_update_time(),
            }
            for col_name, value in attr_map.items():
                if col_name in field_indices:
                    feature.SetField(field_indices[col_name], value)

            layer.CreateFeature(feature)
            feature = None

        # Commit all at once → huge speed boost
        layer.CommitTransaction()

    except Exception as e:
        # Something went wrong – rollback to keep file consistent
        layer.RollbackTransaction()
        ds = None
        raise RuntimeError(f"Insert failed: {e}")

    # Close the datasource
    ds = None

def run_process():
    start = time.time()
    pilots = get_vatsim_data()
    save_to_gpkg(pilots)
    end = time.time()
    print(f"Data {datetime.strftime(vatsim_data.timestamp, '%H:%M:%S')}  |  {len(pilots)} pilots  |   Runtime: {end-start:.2f} secs. ", end='')

# ---------- Continuous loop ----------
import signal

def graceful_exit(signum, frame):
    print("\nShutting down gracefully...")
    sys.exit(0)

# Catch Ctrl+C (SIGINT) to exit cleanly
signal.signal(signal.SIGINT, graceful_exit)

print("Recording started – fetching every 15 seconds. Press Ctrl+C to stop.")

while True:
    print(f"{datetime.strftime(datetime.now(), '%H:%M:%S')} - ", end='')
    time_start = time.time()
    try:
        run_process()
    except Exception as e:
        # Log the error and continue – don't crash the loop
        print(f"Error during cycle: {e}", file=sys.stderr)
    time_end = time.time()

    # Wait 15 seconds until the next fetch
    sleep_time = 15 - max(0,min(15,(time_end - time_start)))
    print(f"Sleeing for {sleep_time:.2f} secs")
    time.sleep(sleep_time)