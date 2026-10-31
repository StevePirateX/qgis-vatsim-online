import os
from typing import List

from osgeo import gdal

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
    QgsMessageLog,
    Qgis,
)

from .pilot import Pilot
from .history_files import resolve_base, daily_gpkg_path, today_date, DEFAULT_BASE_NAME
from .history_recorder import DailyHistory, configure_sqlite
from .stationary_filter import STATIONARY_MAX_GROUNDSPEED, STATIONARY_MOVE_TOLERANCE_M
from .history_recorder_client import RecorderProcessClient, RecorderUnavailable

TABLE_NAME = 'vatsim_history'
PRETTY_NAME = "VATSIM History"

# The history layer only draws when the map is zoomed in past this scale denominator
# (1:10,000,000 here). Set to None to draw at every scale.
HISTORY_MIN_SCALE = 10_000_000

# Record history in a separate process (history_recorder.py) so a slow GeoPackage write can
# never freeze QGIS. If no Python with GDAL can be found, it falls back to recording inside QGIS.
USE_RECORDER_PROCESS = True

# Write-ahead logging lets QGIS read the file while the worker writes. If history writes are
# slow, try False to see whether journal mode is involved.
USE_WAL = True

# SQLite "synchronous" level for history writes. With WAL, NORMAL stops every commit waiting for
# the disk to confirm the write (the file cannot be corrupted by a QGIS crash; only the last few
# seconds could be lost if the whole machine lost power). SQLite's default is FULL, which is
# slower. Set to None to leave SQLite's default.
SQLITE_SYNCHRONOUS = "NORMAL"

# Convert each finished day's GeoPackage to GeoParquet, in the recorder process (never inside
# QGIS). Needs the geoparquet_io package in the Python that runs the recorder; if it isn't
# installed the GeoPackages are simply kept. Whether the GeoPackage is deleted afterwards is
# DELETE_GPKG_AFTER_CONVERT in geoparquet_handler.py.
CONVERT_TO_GEOPARQUET = True


class HistoryLayer:
    """Where the history lives, and how to look at it. MAIN THREAD ONLY.

    History is written as one GeoPackage per day: <folder>/<base name>-YYYY-MM-DD.gpkg. While
    recording, today's file is deliberately NOT loaded into QGIS: a layer that QGIS has open on
    the file being written can make the whole window wait whenever the recorder commits.
    Recording is done by HistoryWriter, which creates the files itself. Call load_into_project()
    to look at today's history once recording has stopped.
    """

    def __init__(self, history_path: str = None, table_name=TABLE_NAME, pretty_name: str = PRETTY_NAME):
        """
        :param history_path: What was entered in the history file box. A .gpkg path gives the
            folder and base name to use (a date in the name is ignored); a folder gives the folder;
            empty means the project folder (or the home folder if the project isn't saved).
        :param table_name: Table in the GeoPackage
        :param pretty_name: Layer name used when the history is loaded into the project
        """
        default_folder = QgsProject.instance().homePath() or os.path.expanduser("~")
        self.folder, self.base_name = resolve_base(history_path, default_folder)
        if not (history_path or "").strip():
            QgsMessageLog.logMessage(
                f"No history location specified. Using the default folder: {self.folder}",
                level=Qgis.MessageLevel.Info)
        self.table_name = table_name
        self.pretty_name = pretty_name

    @property
    def gpkg_path(self) -> str:
        """Today's GeoPackage."""
        return daily_gpkg_path(self.folder, self.base_name, today_date())

    @staticmethod
    def find_existing_layer(path: str = "", project: QgsProject = None) -> QgsVectorLayer | None:
        """Check if a history layer is already loaded in the project."""
        project = project or QgsProject.instance()
        for layer in project.mapLayers().values():
            if (isinstance(layer, QgsVectorLayer) and
                    ((layer.dataProvider().dataSourceUri().startswith(path) or len(path) == 0) and
                     layer.name() == PRETTY_NAME)):
                return layer
        return None

    def unload_from_project(self) -> int:
        """Remove any layers loaded from today's GeoPackage, so QGIS holds no connection to it.
        Returns how many layers were removed."""
        project = QgsProject.instance()
        target = os.path.normcase(os.path.abspath(self.gpkg_path))
        layer_ids = []
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.providerType() == "ogr":
                source = layer.source().partition('|')[0]
                if os.path.normcase(os.path.abspath(source)) == target:
                    layer_ids.append(layer.id())
        if layer_ids:
            project.removeMapLayers(layer_ids)
        return len(layer_ids)

    def load_into_project(self) -> QgsVectorLayer | None:
        """Add today's history table to the project so it can be viewed. Do this when NOT recording."""
        layer = QgsVectorLayer(f"{self.gpkg_path}|layername={self.table_name}", self.pretty_name, "ogr")
        if not layer.isValid():
            return None
        QgsProject.instance().addMapLayer(layer)
        self._apply_default_style(layer)
        return layer

    @staticmethod
    def _apply_default_style(layer: QgsVectorLayer, aircraft_colour: QColor = QColor(0, 255, 255)):
        """A deliberately cheap style: small flat circles, no font glyph, no rotation, no labels.

        The history layer can hold hundreds of thousands of points, so anything expensive per
        point (font markers, data-defined rotation, outlines, labels) makes the canvas crawl.
        """
        colour = QColor(aircraft_colour)
        colour.setAlpha(160)
        symbol = QgsMarkerSymbol.createSimple({
            'name': 'circle',
            'size': '1.0',
            'color': colour.name(QColor.NameFormat.HexArgb),
            'outline_style': 'no',
        })
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        layer.setLabelsEnabled(False)

        # Only draw when zoomed in enough for the points to be useful
        if HISTORY_MIN_SCALE:
            layer.setScaleBasedVisibility(True)
            layer.setMinimumScale(HISTORY_MIN_SCALE)
        layer.triggerRepaint()


class InProcessHistoryWriter:
    """Fallback: records history over a plain OGR connection inside the worker thread.

    open(), write() and close() must all be called from the SAME worker thread. It never
    touches a QgsVectorLayer. The recording itself is history_recorder.DailyHistory, the same
    code the separate recorder process and the standalone worker use. GeoParquet conversion is
    NOT done here (it is heavy and must not run inside QGIS); finished days are converted the
    next time the recorder process or the standalone worker runs.
    """

    in_process = True

    def __init__(self, folder: str, base_name: str, table_name: str = TABLE_NAME,
                 stationary_max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M, record_every_n: int = 1, log=None):
        self._daily = DailyHistory(folder, base_name, table_name, stationary_max_groundspeed,
                                   move_tolerance_m, record_every_n, convert=False, log=log)
        self.last_extended = 0   # unchanged stationary records extended by the last write
        self.last_timing = None  # (begin, insert loop, commit) in seconds, for debug logging

    def set_logger(self, log, verbose: bool = False):
        pass

    def open(self):
        # Thread-local, so it doesn't change anything for the rest of QGIS
        configure_sqlite(USE_WAL, SQLITE_SYNCHRONOUS, thread_local=True)
        self._daily.open()

    def write(self, pilots: List[Pilot], timestamp: str) -> int:
        """Record one snapshot. Returns the number of new rows written."""
        stats = self._daily.write(pilots, timestamp)
        self.last_extended = stats["extended"]
        self.last_timing = (stats["begin"], stats["insert"], stats["commit"])
        return stats["written"]

    def close(self):
        self._daily.close(wait_for_conversion=False)
        if USE_WAL:
            gdal.SetThreadLocalConfigOption("OGR_SQLITE_JOURNAL", None)
            gdal.SetThreadLocalConfigOption("OGR_SQLITE_SYNCHRONOUS", None)


class HistoryWriter:
    """Records pilot positions to the daily history files without ever blocking QGIS.

    Normally the work is done by a separate process (history_recorder.py), so neither Python's
    lock nor the disk can hold up the QGIS window. That process also converts each finished
    day's GeoPackage to GeoParquet. If it cannot be started, this falls back to
    InProcessHistoryWriter (a plain OGR connection inside the worker thread, without conversion).

    open(), write() and close() must all be called from the same worker thread.
    """

    def __init__(self, folder: str, base_name: str = DEFAULT_BASE_NAME, table_name: str = TABLE_NAME,
                 stationary_max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M, record_every_n: int = 1):
        self.folder = folder
        self.base_name = base_name
        self.table_name = table_name
        self.stationary_max_groundspeed = stationary_max_groundspeed
        self.move_tolerance_m = move_tolerance_m
        self.record_every_n = record_every_n
        self._impl = None
        self._log = None
        self._verbose = False

    def set_logger(self, log, verbose: bool = False):
        """log(message, critical=False) is called for status and error messages."""
        self._log = log
        self._verbose = verbose

    def _say(self, message: str, critical: bool = False):
        if self._log:
            self._log(message, critical)

    def open(self):
        if USE_RECORDER_PROCESS:
            client = RecorderProcessClient(
                self.folder, self.base_name, self.table_name, self.stationary_max_groundspeed,
                self.move_tolerance_m, self.record_every_n, USE_WAL, SQLITE_SYNCHRONOUS,
                convert=CONVERT_TO_GEOPARQUET, log=self._log, verbose=self._verbose)
            try:
                client.open()
                self._impl = client
                self._say("History is being recorded by a separate process")
                return
            except RecorderUnavailable as e:
                self._say(f"Separate history recorder unavailable ({e}); recording inside QGIS instead "
                          f"(finished days will not be converted to GeoParquet until the recorder "
                          f"process or the standalone worker next runs)")
        writer = InProcessHistoryWriter(self.folder, self.base_name, self.table_name,
                                        self.stationary_max_groundspeed, self.move_tolerance_m,
                                        self.record_every_n, log=self._log)
        writer.open()
        self._impl = writer

    def write(self, pilots: List[Pilot], timestamp: str) -> int:
        return self._impl.write(pilots, timestamp)

    def close(self):
        if self._impl is not None:
            self._impl.close()
            self._impl = None

    @property
    def in_process(self) -> bool:
        return isinstance(self._impl, InProcessHistoryWriter)

    @property
    def last_timing(self):
        """Timing of the last write; only meaningful (and only reported) when recording in-process."""
        return self._impl.last_timing if self.in_process else None

    @property
    def last_extended(self) -> int:
        return self._impl.last_extended if self.in_process else 0