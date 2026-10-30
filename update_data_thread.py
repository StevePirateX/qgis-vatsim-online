import time

from qgis.PyQt.QtCore import pyqtSignal, QThread
from qgis.core import Qgis

from .live_aircraft_layer import VatsimAircraftLayer


class VatsimUpdateThread(QThread):
    """Fetches VATSIM data and prepares it for display.

    This thread never touches a QGIS layer. It emits a prepared snapshot that the main
    thread applies, and writes history through its own plain OGR connection.
    """
    data_ready = pyqtSignal(object)     # prepared snapshot dict; queued to the main thread
    log_message = pyqtSignal(str, int)  # thread-safe logging (level sent as int)

    def __init__(self, vatsim_data, refresh_rate, history_writer=None, hidden_vatsim_id: int = 0,
                 debug: bool = False, field_idx: dict = None, field_count: int = 0):
        super().__init__()
        self.vatsim_data = vatsim_data
        self.refresh_rate = refresh_rate
        self.history_writer = history_writer
        self.hidden_vatsim_id = hidden_vatsim_id
        self.debug = debug
        # Where each VATSIM field lives in the target layer (plain data, no layer access)
        self.field_idx = dict(field_idx or {})
        self.field_count = field_count
        self._running = False

    def _log(self, message, level=Qgis.MessageLevel.Info):
        self.log_message.emit(message, int(level))

    def _writer_log(self, message, critical=False):
        """Logger handed to the history writer (may be called from its own helper thread)."""
        self._log(message, Qgis.MessageLevel.Critical if critical else Qgis.MessageLevel.Info)

    def run(self):
        self._running = True
        last_ts = None
        try:
            if self.history_writer:
                self.history_writer.set_logger(self._writer_log, self.debug)
                self.history_writer.open()
            while self._running:
                start = time.perf_counter()
                try:
                    self.vatsim_data.fetch_vatsim_data()
                    t_fetch = time.perf_counter()

                    ts = self.vatsim_data.timestamp
                    if ts != last_ts:           # skip if the feed hasn't changed
                        last_ts = ts
                        pilots = self.vatsim_data.pilots   # replaced, never mutated, per fetch

                        prepared = VatsimAircraftLayer.prepare_snapshot(
                            pilots, self.hidden_vatsim_id, self.field_idx, self.field_count)
                        t_prepare = time.perf_counter()
                        self.data_ready.emit(prepared)  # display first, history second

                        written = 0
                        if self.history_writer:
                            if self.debug:
                                self._log("worker: history write started")
                            written = self.history_writer.write(pilots, self.vatsim_data.get_update_time())
                        t_done = time.perf_counter()

                        if self.debug:
                            history_ms = 1000 * (t_done - t_prepare)
                            if getattr(self.history_writer, "in_process", False):
                                extended = self.history_writer.last_extended
                                detail = ""
                                timing = self.history_writer.last_timing
                                if timing:
                                    detail = (f" [begin {1000 * timing[0]:.0f} / insert {1000 * timing[1]:.0f}"
                                              f" / commit {1000 * timing[2]:.0f} ms]")
                                history_text = (f"history {history_ms:.0f} ms ({written} rows, "
                                                f"{extended} unchanged extended){detail}")
                            else:
                                # separate recorder process: the write itself is reported by the recorder
                                history_text = f"history handoff {history_ms:.0f} ms"
                            self._log(
                                f"worker: fetch+parse {1000 * (t_fetch - start):.0f} ms, "
                                f"prepare {1000 * (t_prepare - t_fetch):.0f} ms, {history_text}"
                            )
                except Exception as e:
                    self._log(f"Update failed: {e}", Qgis.MessageLevel.Critical)

                # Sleep in small chunks so Stop responds quickly
                end = start + self.refresh_rate
                while self._running and time.perf_counter() < end:
                    self.msleep(100)
        except Exception as e:
            self._log(f"Thread error: {e}", Qgis.MessageLevel.Critical)
        finally:
            if self.history_writer:
                self.history_writer.close()

    def stop(self):
        """Safely stop the thread."""
        self._running = False
        self.wait()