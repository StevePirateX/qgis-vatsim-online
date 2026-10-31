"""History recording: the one implementation shared by the QGIS plugin, the recorder process and
the standalone worker. Also the entry point of the recorder process the plugin starts.

Writing to a GeoPackage can take several seconds, and doing that inside the QGIS process was
freezing the interface for the whole write. The plugin therefore runs this file as its own
process, so QGIS is never involved.

What it provides
  * write_snapshot()  - writes one VATSIM update into a GeoPackage table.
  * DailyHistory      - one GeoPackage per day. When the day changes it starts a new file and
                        converts the finished day's file to GeoParquet in the background.

Recorder process protocol: one JSON message per line.
  plugin -> recorder (stdin):  {"type":"snapshot","ts":"...","pilots":[[callsign, cid, lat, lon,
                               alt, heading, groundspeed, departure, arrival, aircraft, cruise,
                               route], ...]}   and   {"type":"quit"}
  recorder -> plugin (stdout): {"type":"ready"}, {"type":"stats",...}, {"type":"log",...},
                               {"type":"error",...}
If the plugin goes away (stdin closes) the recorder finishes its current work and exits.
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone

try:  # imported as part of the QGIS plugin package
    from .stationary_filter import (StationaryFilter, EXTEND, STATIONARY_MAX_GROUNDSPEED,
                                    STATIONARY_MOVE_TOLERANCE_M)
    from .history_table import open_history_layer, LAST_SEEN
    from .history_files import daily_gpkg_path, today_date
    from .geoparquet_handler import FinishedDayConverter, DELETE_GPKG_AFTER_CONVERT
except ImportError:  # run as a script (the recorder process or the standalone worker)
    from stationary_filter import (StationaryFilter, EXTEND, STATIONARY_MAX_GROUNDSPEED,
                                   STATIONARY_MOVE_TOLERANCE_M)
    from history_table import open_history_layer, LAST_SEEN
    from history_files import daily_gpkg_path, today_date
    from geoparquet_handler import FinishedDayConverter, DELETE_GPKG_AFTER_CONVERT

from osgeo import ogr, gdal

Pilot = namedtuple("Pilot", "callsign cid latitude longitude altitude heading groundspeed "
                            "departure arrival aircraft cruise route")


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


class DailyHistory:
    """Records history into one GeoPackage per day.

    * The file for a day is <folder>/<base name>-YYYY-MM-DD.gpkg and is created when needed.
    * When the day changes, the finished file is closed, a fresh file is started (with a fresh
      stationary filter, so each day's file stands alone) and the finished day is converted to
      GeoParquet on a background thread.
    * Any older daily files still lying around (for example from a day this program wasn't
      running at midnight) are converted too, when it starts and at each rollover.

    Not thread-safe: call it from a single thread. `log(message, critical=False)` may be called
    from the conversion thread as well.
    """

    def __init__(self, folder: str, base_name: str, table_name: str,
                 max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M,
                 record_every_n: int = 1, convert: bool = True,
                 delete_converted: bool = DELETE_GPKG_AFTER_CONVERT, log=None, today=today_date):
        self.folder = folder
        self.base_name = base_name
        self.table_name = table_name
        self.record_every_n = max(1, record_every_n)
        self._max_groundspeed = max_groundspeed
        self._move_tolerance_m = move_tolerance_m
        self._today = today
        self._log = log
        self._converter = FinishedDayConverter(table_name, delete_converted, log) if convert else None
        self._filter = StationaryFilter(max_groundspeed, move_tolerance_m)
        self._count = 0
        self._day = None
        self._caught_up = False
        self._ds = self._layer = self._defn = None
        self._idx = {}

    @property
    def current_path(self):
        """The GeoPackage being written to now (None before the first write)."""
        return None if self._day is None else daily_gpkg_path(self.folder, self.base_name, self._day)

    def open(self):
        """Open today's file now (instead of at the first write), so problems show up early."""
        self._ensure_open(self._today())

    def write(self, pilots, timestamp: str) -> dict:
        """Record one snapshot in today's file. Returns the statistics from write_snapshot()."""
        self._count += 1
        if (self._count - 1) % self.record_every_n != 0:
            return dict(written=0, extended=0, begin=0.0, insert=0.0, commit=0.0)
        self._ensure_open(self._today())
        return write_snapshot(self._ds, self._layer, self._defn, self._idx, self._filter, pilots, timestamp)

    def reset(self):
        """Close the file; the next write reopens it (use after an error, in case the file went away)."""
        self._close_file()

    def close(self, wait_for_conversion: bool = True):
        self._close_file()
        if self._converter is not None and wait_for_conversion:
            self._converter.wait()

    # ------------------------------------------------------------------ internals

    def _ensure_open(self, day):
        if self._ds is not None and day == self._day:
            return
        self._close_file()
        if self._day is not None and day != self._day:
            self._filter = StationaryFilter(self._max_groundspeed, self._move_tolerance_m)
            self._caught_up = False
            self._say(f"A new day has started: recording to a new file for {day:%Y-%m-%d}")
        path = daily_gpkg_path(self.folder, self.base_name, day)
        ds, layer = open_history_layer(path, self.table_name)  # raises if it can't (e.g. folder missing)
        self._ds, self._layer = ds, layer
        self._defn = layer.GetLayerDefn()
        self._idx = {self._defn.GetFieldDefn(i).GetName(): i for i in range(self._defn.GetFieldCount())}
        self._day = day
        if not self._caught_up:
            self._caught_up = True
            if self._converter is not None:
                self._converter.convert_older_than(self.folder, self.base_name, day)

    def _close_file(self):
        self._layer = None
        self._ds = None  # closing the dataset flushes everything to disk

    def _say(self, message: str, critical: bool = False):
        if self._log:
            self._log(message, critical)


# ---------------------------------------------------------------------- recorder process

_reply_lock = threading.Lock()


def reply(**message):
    with _reply_lock:  # the conversion thread reports progress too
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _relay(message: str, critical: bool = False):
    reply(type="error" if critical else "log", message=message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--max-speed", type=float, required=True)
    parser.add_argument("--tolerance", type=float, required=True)
    parser.add_argument("--every", type=int, default=1)
    parser.add_argument("--wal", default="1")
    parser.add_argument("--sync", default="")
    parser.add_argument("--convert", default="1")
    args = parser.parse_args()

    gdal.UseExceptions()
    configure_sqlite(args.wal == "1", args.sync)

    daily = DailyHistory(args.folder, args.base, args.table, args.max_speed, args.tolerance,
                         args.every, convert=args.convert == "1", log=_relay)
    try:
        daily.open()  # creates today's GeoPackage and table if needed, so QGIS never has to
    except Exception as e:
        reply(type="error", message=f"history recorder could not start in {args.folder}: {e}")
        return 2

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
        try:
            pilots = [Pilot(*row) for row in message["pilots"]]
            reply(type="stats", **daily.write(pilots, message["ts"]))
        except Exception as e:
            reply(type="error", message=f"history write failed: {e}")
            daily.reset()  # reopen the file on the next snapshot, in case it went away

    daily.close(wait_for_conversion=True)  # finish any GeoParquet conversion before exiting
    return 0


if __name__ == "__main__":
    sys.exit(main())