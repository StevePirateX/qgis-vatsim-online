import time
from datetime import datetime
from qgis.PyQt.QtCore import pyqtSignal, QThread
from qgis.core import Qgis

class VatsimUpdateThread(QThread):
    data_ready = pyqtSignal(object)      # list[Pilot]
    log_message = pyqtSignal(str, int)

    def __init__(self, vatsim_data, refresh_rate, history_writer=None, debug=False):
        super().__init__()
        self.vatsim_data = vatsim_data
        self.refresh_rate = refresh_rate
        self.history_writer = history_writer
        self.debug = debug
        self._running = False

    def run(self):
        self._running = True
        previous_timestamp = None
        try:
            if self.history_writer:
                self.history_writer.open()
            while self._running:
                start = time.time()
                try:
                    self.vatsim_data.fetch_vatsim_data()
                    timestamp = self.vatsim_data.timestamp
                    if timestamp != previous_timestamp:
                        previous_timestamp = timestamp
                        pilots = self.vatsim_data.pilots
                        self.data_ready.emit(pilots)
                        if self.history_writer:
                            self.history_writer.write(pilots, self.vatsim_data.get_update_time())
                except Exception as e:
                    self.log_message.emit(f"Update failed: {e}", int(Qgis.MessageLevel.Critical))

                end = start + self.refresh_rate
                while self._running and time.time() < end:
                    self.msleep(100)
        finally:
            if self.history_writer:
                self.history_writer.close()

    def stop(self):
        self._running = False
        self.wait()