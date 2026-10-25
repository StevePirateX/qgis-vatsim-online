"""Plugin-side client for the history recorder process (see history_recorder.py).

Pure Python (no QGIS), so it can be tested on its own. Messages go to the `log` callable as
log(message, critical=False).
"""
import json
import os
import shutil
import subprocess
import sys
import threading

HELPER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history_recorder.py")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # stops a console flashing up on Windows
_python_path = None  # cached result of the search below


class RecorderUnavailable(Exception):
    """The separate recorder process could not be started."""


def _child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    return env


def _candidate_pythons():
    exe = sys.executable or ""
    if os.path.basename(exe).lower().startswith("python"):
        yield exe
    # QGIS embeds Python, so sys.executable is usually QGIS itself; look for the real one
    yield os.path.join(sys.exec_prefix, "bin", "python3")
    yield os.path.join(sys.exec_prefix, "python.exe")
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            yield found


def find_python_with_gdal():
    """Return the path of a Python that can import GDAL's osgeo package, or None."""
    global _python_path
    if _python_path and os.path.exists(_python_path):
        return _python_path
    tried = set()
    for candidate in _candidate_pythons():
        if candidate in tried or not os.path.exists(candidate):
            continue
        tried.add(candidate)
        try:
            result = subprocess.run([candidate, "-c", "from osgeo import ogr, gdal"],
                                    capture_output=True, timeout=30,
                                    creationflags=_NO_WINDOW, env=_child_env())
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            _python_path = candidate
            return candidate
    return None


class RecorderProcessClient:
    """Sends pilot snapshots to the recorder process. write() never waits for the database."""

    def __init__(self, gpkg_path, table_name, max_groundspeed, move_tolerance_m, record_every_n=1,
                 use_wal=True, synchronous="NORMAL", log=None, verbose=False):
        self.gpkg_path = gpkg_path
        self.table_name = table_name
        self.max_groundspeed = max_groundspeed
        self.move_tolerance_m = move_tolerance_m
        self.record_every_n = record_every_n
        self.use_wal = use_wal
        self.synchronous = synchronous
        self.verbose = verbose
        self._log = log
        self._proc = None
        self._ready = threading.Event()
        self._dead = False
        self._reported_dead = False
        self._noise_lines = 0
        self.last_skipped = 0
        self.last_timing = None  # (begin, insert, commit) of the most recent write, in seconds

    def _say(self, message: str, critical: bool = False):
        if self._log:
            self._log(message, critical)

    def open(self, timeout: float = 30.0):
        python = find_python_with_gdal()
        if python is None:
            raise RecorderUnavailable("no Python installation with GDAL (osgeo) was found")
        command = [python, HELPER_SCRIPT,
                   "--gpkg", self.gpkg_path, "--table", self.table_name,
                   "--max-speed", str(self.max_groundspeed),
                   "--tolerance", str(self.move_tolerance_m),
                   "--every", str(self.record_every_n),
                   "--wal", "1" if self.use_wal else "0",
                   "--sync", self.synchronous or ""]
        try:
            self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                          bufsize=1, creationflags=_NO_WINDOW, env=_child_env())
        except OSError as e:
            raise RecorderUnavailable(f"could not start the recorder: {e}")

        threading.Thread(target=self._read_replies, args=(self._proc,), daemon=True).start()
        if not self._ready.wait(timeout) or self._dead:
            self._kill()
            raise RecorderUnavailable("the recorder process did not start")

    def _read_replies(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    if self._noise_lines < 20:  # warnings or a traceback from the recorder
                        self._noise_lines += 1
                        self._say(f"history recorder output: {line[:300]}")
                    continue
                kind = message.get("type")
                if kind == "ready":
                    self._ready.set()
                elif kind == "stats":
                    self.last_skipped = message.get("skipped", 0)
                    self.last_timing = (message.get("begin", 0), message.get("insert", 0),
                                        message.get("commit", 0))
                    if self.verbose:
                        begin, insert, commit = self.last_timing
                        self._say(f"history recorder (separate process): {message.get('written', 0)} rows, "
                                  f"{self.last_skipped} unchanged skipped "
                                  f"[begin {1000 * begin:.0f} / insert {1000 * insert:.0f} / "
                                  f"commit {1000 * commit:.0f} ms]")
                elif kind == "error":
                    self._say(message.get("message", "history recorder error"), critical=True)
        finally:
            self._dead = True
            self._ready.set()

    def write(self, pilots, timestamp: str) -> int:
        """Hand a snapshot to the recorder. Returns immediately; the result is reported later."""
        if self._dead or self._proc is None or self._proc.poll() is not None:
            if not self._reported_dead:
                self._reported_dead = True
                self._say("The history recorder process has stopped; history is no longer being recorded",
                          critical=True)
            return 0
        rows = [[p.callsign, p.cid, p.latitude, p.longitude, p.altitude, p.heading, p.groundspeed,
                 p.departure, p.arrival, p.aircraft, p.cruise, p.route] for p in pilots]
        line = json.dumps({"type": "snapshot", "ts": timestamp, "pilots": rows}, separators=(",", ":"))
        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            self._dead = True
            self._say(f"Could not send to the history recorder: {e}", critical=True)
        return 0

    def close(self):
        """Ask the recorder to finish and exit. Does not wait: a final slow write carries on in
        the background without holding anything up."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(json.dumps({"type": "quit"}) + "\n")
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    @staticmethod
    def _reap(proc):
        try:
            proc.wait(timeout=300)  # allow a slow final write
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass