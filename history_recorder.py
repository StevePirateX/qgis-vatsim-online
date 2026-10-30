"""History recorder: runs as its own process, started by the QGIS plugin.

Writing to a GeoPackage can take several seconds, and doing that inside the QGIS process was
freezing the interface for the whole write. This script does the writing in a separate process,
so QGIS is never involved. Its write_snapshot() is also what the plugin's in-process fallback and
the standalone worker use, so there is only one implementation of how history is written.

Protocol: one JSON message per line.
  plugin -> recorder (stdin):  {"type":"snapshot","ts":"...","pilots":[[callsign, cid, lat, lon,
                               alt, heading, groundspeed, departure, arrival, aircraft, cruise,
                               route], ...]}   and   {"type":"quit"}
  recorder -> plugin (stdout): {"type":"ready"}, {"type":"stats",...}, {"type":"error",...}
If the plugin goes away (stdin closes) the recorder finishes its current write and exits.
"""
import argparse
import json
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone

try:  # imported as part of the QGIS plugin package
    from .stationary_filter import StationaryFilter, EXTEND
    from .history_table import open_history_layer, LAST_SEEN
except ImportError:  # run as a script (the recorder process or the standalone worker)
    from stationary_filter import StationaryFilter, EXTEND
    from history_table import open_history_layer, LAST_SEEN

from osgeo import ogr, gdal

Pilot = namedtuple("Pilot", "callsign cid latitude longitude altitude heading groundspeed "
                            "departure arrival aircraft cruise route")


def reply(**message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def configure_sqlite(wal: bool, synchronous: str, thread_local: bool = False):
    """GDAL settings for writing a GeoPackage that QGIS may be reading at the same time.

    WAL lets readers carry on while we write; NORMAL stops every commit waiting for the disk
    (safe against crashes of this program or QGIS; only the last moments could be lost if the
    whole machine lost power). thread_local applies the settings to the calling thread only,
    which is what to use inside QGIS so nothing else in QGIS is affected.
    """
    if not wal:
        return
    set_option = gdal.SetThreadLocalConfigOption if thread_local else gdal.SetConfigOption
    set_option("OGR_SQLITE_JOURNAL", "WAL")
    if synchronous:
        set_option("OGR_SQLITE_SYNCHRONOUS", synchronous)


def gpkg_datetime(timestamp: str) -> str:
    """Convert an ISO timestamp to the exact text form GDAL stores date/times in
    (UTC, millisecond precision, trailing Z), so rows we insert and rows we update with SQL
    use the same format."""
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def _extend_records(ds, layer, fids, last_seen: str, chunk_size: int = 500):
    """Push last_seen forward on many rows with a few UPDATE statements."""
    table = layer.GetName()
    fid_column = layer.GetFIDColumn() or "fid"
    for start in range(0, len(fids), chunk_size):
        ids = ",".join(str(int(fid)) for fid in fids[start:start + chunk_size])
        result = ds.ExecuteSQL(
            f'UPDATE "{table}" SET "{LAST_SEEN}" = \'{last_seen}\' WHERE "{fid_column}" IN ({ids})')
        if result is not None:
            ds.ReleaseResultSet(result)


def write_snapshot(ds, layer, defn, idx, flt, pilots, timestamp):
    """Record one snapshot in a single transaction and return timing/count statistics.

    * A pilot the filter says to RECORD gets a new row (timestamp = last_seen = this snapshot).
    * A stationary pilot whose state hasn't changed has the last_seen of its existing row pushed
      forward instead, so no new row is written.

    pilots: any objects with the attributes callsign, cid, latitude, longitude, altitude,
    heading, groundspeed, departure, arrival, aircraft, cruise and route (the plugin's Pilot
    class works, as does the Pilot tuple above).
    """
    ts = gpkg_datetime(timestamp)
    written = 0
    to_extend = []   # ids of the rows to extend
    recorded = {}    # callsign -> (state, row id); remembered only if the transaction commits

    t0 = time.perf_counter()
    layer.StartTransaction()
    t1 = time.perf_counter()
    try:
        for p in pilots:
            action, state = flt.check(p)
            if action == EXTEND:
                to_extend.append(flt.open_fid(p.callsign))
                continue
            feature = ogr.Feature(defn)
            point = ogr.Geometry(ogr.wkbPoint25D)
            point.AddPoint(p.longitude, p.latitude, p.altitude)
            feature.SetGeometry(point)
            for name, value in (("callsign", p.callsign), ("cid", p.cid), ("heading", p.heading),
                                ("groundspeed", p.groundspeed), ("departure", p.departure),
                                ("arrival", p.arrival), ("aircraft", p.aircraft),
                                ("timestamp", ts), (LAST_SEEN, ts)):
                feature.SetField(idx[name], value)
            layer.CreateFeature(feature)
            recorded[p.callsign] = (state, feature.GetFID())
            written += 1
        if to_extend:
            _extend_records(ds, layer, to_extend, ts)
        t2 = time.perf_counter()
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        raise
    t3 = time.perf_counter()

    flt.commit(recorded, {p.callsign for p in pilots})
    return dict(written=written, extended=len(to_extend), begin=t1 - t0, insert=t2 - t1, commit=t3 - t2)


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
    configure_sqlite(args.wal == "1", args.sync)

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
            reply(type="stats", written=0, extended=0, begin=0, insert=0, commit=0)
            continue
        try:
            pilots = [Pilot(*row) for row in message["pilots"]]
            reply(type="stats", **write_snapshot(ds, layer, defn, idx, flt, pilots, message["ts"]))
        except Exception as e:
            reply(type="error", message=f"history write failed: {e}")

    layer = None
    ds = None  # closing the dataset flushes everything to disk
    return 0


if __name__ == "__main__":
    sys.exit(main())