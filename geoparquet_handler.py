"""Converting finished daily history GeoPackages to GeoParquet, to save disk space.

Used by the recorder process and the standalone worker. Nothing here runs when the module is
imported, and `geoparquet_io` is only imported when a conversion actually happens, so everything
else keeps working if it isn't installed.

A finished GeoPackage is only deleted after the GeoParquet has been written and its row count
checked against the GeoPackage.
"""
import os
import queue
import threading
import time

try:  # imported as part of the QGIS plugin package
    from .history_files import finished_gpkgs, parquet_path_for
except ImportError:  # run as a script
    from history_files import finished_gpkgs, parquet_path_for

# Delete the GeoPackage once its GeoParquet has been written and checked.
DELETE_GPKG_AFTER_CONVERT = True

LOCK_STALE_SECONDS = 2 * 3600          # a conversion lock older than this is assumed to be left over
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")  # SQLite's companion files


def geoparquet_available() -> bool:
    try:
        import geoparquet_io  # noqa: F401
        return True
    except ImportError:
        return False


def convert_to_geoparquet(input_file: str, output_file: str, layer: str = None) -> int:
    """Convert the input gpkg to geoparquet.

    :param input_file: input geopackage file
    :param output_file: output geoparquet
    :param layer: table to convert (only needed if the GeoPackage has more than one)
    :return: the number of rows converted
    """
    import geoparquet_io as gpio  # imported here so the rest of the project works without it

    table = gpio.convert(input_file, layer=layer) if layer else gpio.convert(input_file)
    table.write(output_file, overwrite=True)
    return table.num_rows


def count_rows(gpkg_path: str, table_name: str) -> int:
    """Exact number of rows in the GeoPackage's table."""
    from osgeo import ogr
    ds = ogr.Open(gpkg_path)
    if ds is None:
        raise RuntimeError(f"could not open {gpkg_path}")
    result = ds.ExecuteSQL(f'SELECT COUNT(*) FROM "{table_name}"')
    count = int(result.GetNextFeature().GetField(0))
    ds.ReleaseResultSet(result)
    ds = None
    return count


def _claim(lock_path: str) -> bool:
    """Take the conversion lock for a file. False if someone else is converting it."""
    for _ in range(2):
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) < LOCK_STALE_SECONDS:
                    return False
                os.remove(lock_path)  # stale: a previous run died mid-conversion
            except OSError:
                return False
            continue
        with os.fdopen(handle, "w") as lock:
            lock.write(f"{os.getpid()}\n")
        return True
    return False


def _remove_quietly(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def convert_finished_gpkg(gpkg_path: str, table_name: str, delete_source: bool = DELETE_GPKG_AFTER_CONVERT,
                          log=None) -> bool:
    """Convert one finished daily GeoPackage to GeoParquet. Returns True if it was converted.

    * Written to a temporary file first, so a half-written GeoParquet is never left behind.
    * The row count must match the GeoPackage's, or nothing is replaced or deleted.
    * A lock file stops two programs converting the same file at once.
    """
    def say(message, critical=False):
        if log:
            log(message, critical)

    name = os.path.basename(gpkg_path)
    final = parquet_path_for(gpkg_path)
    partial = final[:-len(".parquet")] + ".partial.parquet"
    lock = gpkg_path + ".converting"

    if not _claim(lock):
        say(f"{name} is already being converted by another process; skipping it")
        return False
    try:
        expected = count_rows(gpkg_path, table_name)
        if expected == 0:
            say(f"{name} holds no rows; nothing to convert")
            if delete_source:
                _remove_gpkg(gpkg_path)
            return False

        say(f"Converting {name} ({expected:,} rows) to GeoParquet...")
        started = time.time()
        _remove_quietly(partial)
        rows = convert_to_geoparquet(gpkg_path, partial, layer=table_name)
        if rows != expected:
            raise RuntimeError(f"row count mismatch: {rows:,} rows in the GeoParquet but {expected:,} in the GeoPackage")

        os.replace(partial, final)
        saved = ""
        if delete_source:
            before = os.path.getsize(gpkg_path)
            _remove_gpkg(gpkg_path)
            saved = f"; GeoPackage removed ({_size(before)} -> {_size(os.path.getsize(final))})"
        say(f"Converted {name} to {os.path.basename(final)}: {rows:,} rows in {time.time() - started:.0f} s{saved}")
        return True
    except Exception as e:
        _remove_quietly(partial)
        say(f"Could not convert {name} to GeoParquet (the GeoPackage was kept): {e}", critical=True)
        return False
    finally:
        _remove_quietly(lock)


def _size(num_bytes: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "bytes" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024


def _remove_gpkg(gpkg_path: str):
    os.remove(gpkg_path)
    for suffix in SIDECAR_SUFFIXES:
        _remove_quietly(gpkg_path + suffix)


class FinishedDayConverter:
    """Converts finished days' GeoPackages on a background thread, one at a time, so recording
    carries on while a big file is being converted."""

    def __init__(self, table_name: str, delete_source: bool = DELETE_GPKG_AFTER_CONVERT, log=None):
        self.table_name = table_name
        self.delete_source = delete_source
        self._log = log
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None
        self._warned_missing = False

    def _say(self, message: str, critical: bool = False):
        if self._log:
            self._log(message, critical)

    def convert_older_than(self, folder: str, base_name: str, today):
        """Queue every daily GeoPackage for a day before `today` (including ones left over from
        earlier runs that never got converted)."""
        paths = finished_gpkgs(folder, base_name, today)
        if not paths:
            return
        if not geoparquet_available():
            if not self._warned_missing:
                self._warned_missing = True
                self._say("The geoparquet_io package is not installed in this Python, so finished days "
                          "can't be converted to GeoParquet yet. The GeoPackages are being kept.", critical=True)
            return
        for path in paths:
            if not self.delete_source and self._already_converted(path):
                continue
            with self._lock:
                if path in self._queued:
                    continue
                self._queued.add(path)
                self._queue.put(path)
                if self._thread is None:
                    self._thread = threading.Thread(target=self._work, daemon=True, name="geoparquet-converter")
                    self._thread.start()

    @staticmethod
    def _already_converted(gpkg_path: str) -> bool:
        parquet = parquet_path_for(gpkg_path)
        try:
            return os.path.getmtime(parquet) >= os.path.getmtime(gpkg_path)
        except OSError:
            return False

    def _work(self):
        while True:
            with self._lock:
                try:
                    path = self._queue.get_nowait()
                except queue.Empty:
                    self._thread = None  # a new thread is started when more work is queued
                    return
            try:
                convert_finished_gpkg(path, self.table_name, self.delete_source, self._log)
            except Exception as e:  # never let one file stop the others
                self._say(f"Unexpected error converting {os.path.basename(path)}: {e}", critical=True)
            finally:
                with self._lock:
                    self._queued.discard(path)
                self._queue.task_done()

    def busy(self) -> bool:
        return self._queue.unfinished_tasks > 0

    def wait(self, timeout: float = None) -> bool:
        """Wait for queued conversions to finish. Returns False if the timeout ran out first."""
        if self.busy():
            self._say("Waiting for the GeoParquet conversion to finish...")
        deadline = None if timeout is None else time.time() + timeout
        while self.busy():
            if deadline is not None and time.time() > deadline:
                return False
            time.sleep(0.2)
        return True