import time
from qgis.PyQt.QtCore import pyqtSignal, QThread, QMutex
from qgis._core import Qgis

from .vatsim_aircraft_layer import VatsimAircraftLayer
from .history_layer import HistoryLayer

class VatsimUpdateThread(QThread):
    update_complete = pyqtSignal(bool)  # Signal to indicate update completion
    log_message = pyqtSignal(str, int)  # For thread-safe logging

    def __init__(self, vatsim_data, vatsim_layer, refresh_rate, history_layer:HistoryLayer=None, hide_vatsim_id: int=0, debug: bool=False):
        super().__init__()
        self.vatsim_data = vatsim_data
        self.vatsim_layer = vatsim_layer
        self.history_layer = history_layer
        self.refresh_rate = refresh_rate
        self._mutex = QMutex()
        self._running = False
        self.hidden_vatsim_id = hide_vatsim_id
        self.debug = debug

    def run(self):
        self._running = True
        while self._running:
            start_time = time.time()

            try:
                # Perform the update in this thread
                success = self._perform_update()
                self.update_complete.emit(success)
            except Exception as e:
                self.log_message.emit(f"Thread error: {str(e)}", Qgis.MessageLevel.Critical)

            # Calculate remaining time to sleep
            elapsed = time.time() - start_time
            sleep_time = max(0, self.refresh_rate - elapsed)

            # Sleep in chunks so we can respond to stop requests
            for _ in range(int(sleep_time * 10)):
                if not self._running:
                    return
                time.sleep(0.1)

    def _perform_update(self):
        """Actual update logic that runs in the background thread."""
        try:
            if self.debug:
                self.log_message.emit("Fetching VATSIM data...", Qgis.Info)

            self.vatsim_data.fetch_vatsim_data()

            # Lock mutex for layer operations
            self._mutex.lock()
            try:
                result = self.vatsim_layer.full_refresh(self.vatsim_data.pilots, self.hidden_vatsim_id)
                if result and self.debug:
                    self.log_message.emit("Update complete", Qgis.MessageLevel.Success)
                #if self.history_layer is not None:
                    #self.history_layer.full_refresh(self.vatsim_data.pilots)
                return result
            finally:
                self._mutex.unlock()

        except Exception as e:
            self.log_message.emit(f"Update failed: {str(e)}", Qgis.MessageLevel.Critical)
            return False

    def stop(self):
        """Safely stop the thread."""
        self._running = False
        self.wait()