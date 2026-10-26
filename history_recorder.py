"""History recorder: runs as its own process, started by the QGIS plugin.

Writing to a GeoPackage can take several seconds, and doing that inside the QGIS process was
freezing the interface for the whole write. This script does the writing in a separate process,
so QGIS is never involved.

Protocol: one JSON message per line.
  plugin -> recorder (stdin):  {"type":"snapshot","ts":"...","pilots":[[callsign, cid, lat, lon,
                               alt, heading, groundspeed, departure, arrival, aircraft, cruise,
                               route], ...]}   and   {"type":"quit"}
  recorder -> plugin (stdout): {"type":"ready"}, {"type":"stats",...}, {"type":"error",...}
If the plugin goes away (stdin closes) the recorder finishes its current write and exits.
"""
import argparse
import json
import os
import sys
import time
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stationary_filter import StationaryFilter  # noqa: E402
from history_table import open_history_layer  # noqa: E402
from osgeo import ogr, gdal  # noqa: E402

Pilot = namedtuple("Pilot", "callsign cid latitude longitude altitude heading groundspeed "
                            "departure arrival aircraft cruise route")


def reply(**message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def write_snapshot(layer, defn, idx, flt, rows, timestamp):
    """Insert one snapshot in a single transaction and return timing/count statistics."""
    written = skipped = 0
    recorded = {}  # remembered by the filter only if the transaction commits

    t0 = time.perf_counter()
    layer.StartTransaction()
    t1 = time.perf_counter()
    try:
        for row in rows:
            p = Pilot(*row)
            record, state = flt.check(p)
            if not record:
                skipped += 1
                continue
            recorded[p.callsign] = state
            feature = ogr.Feature(defn)
            point = ogr.Geometry(ogr.wkbPoint25D)
            point.AddPoint(p.longitude, p.latitude, p.altitude)
            feature.SetGeometry(point)
            for name, value in (("callsign", p.callsign), ("cid", p.cid), ("heading", p.heading),
                                ("groundspeed", p.groundspeed), ("departure", p.departure),
                                ("arrival", p.arrival), ("aircraft", p.aircraft),
                                ("timestamp", timestamp)):
                feature.SetField(idx[name], value)
            layer.CreateFeature(feature)
            written += 1
        t2 = time.perf_counter()
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        raise
    t3 = time.perf_counter()

    flt.commit(recorded, {row[0] for row in rows})
    return dict(written=written, skipped=skipped, begin=t1 - t0, insert=t2 - t1, commit=t3 - t2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpkg", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--max-speed", type=float, required=True)
    parser.add_argument("--tolerance", type=float, required=True)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--wal", default="1")
    parser.add_argument("--sync", default="")
    args = parser.parse_args()

    gdal.UseExceptions()
    if args.wal == "1":
        gdal.SetConfigOption("OGR_SQLITE_JOURNAL", "WAL")
    if args.sync:
        gdal.SetConfigOption("OGR_SQLITE_PRAGMA", f"synchronous={args.sync}")

    try:
        # creates the GeoPackage and table if they don't exist, so QGIS never has to touch the file
        ds, layer = open_history_layer(args.gpkg, args.table)
    except Exception as e:
        reply(type="error", message=f"history recorder could not open {args.gpkg}: {e}")
        return 2

    defn = layer.GetLayerDefn()
    idx = {defn.GetFieldDefn(i).GetName(): i for i in range(defn.GetFieldCount())}
    flt = StationaryFilter(args.max_speed, args.tolerance)
    every = max(1, args.every)
    count = 0

    reply(type="ready")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        kind = message.get("type")
        if kind == "quit":
            break
        if kind != "snapshot":
            continue

        count += 1
        if (count - 1) % every != 0:
            reply(type="stats", written=0, skipped=0, begin=0, insert=0, commit=0)
            continue
        try:
            reply(type="stats", **write_snapshot(layer, defn, idx, flt, message["pilots"], message["ts"]))
        except Exception as e:
            reply(type="error", message=f"history write failed: {e}")

    layer = None
    ds = None  # closing the dataset flushes everything to disk
    return 0


if __name__ == "__main__":
    sys.exit(main())