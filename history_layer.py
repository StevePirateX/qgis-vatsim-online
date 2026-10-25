import os
import time
from datetime import datetime
from typing import List

from osgeo import ogr, gdal

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsVectorLayer,
    QgsProject,
    QgsField,
    QgsVectorFileWriter,
    QgsMarkerSymbol,
    QgsSingleSymbolRenderer,
    QgsMessageLog,
    Qgis,
)

from .pilot import Pilot
from .history_table import open_history_layer
from .stationary_filter import (StationaryFilter, STATIONARY_MAX_GROUNDSPEED,
                                STATIONARY_MOVE_TOLERANCE_M)
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

# When no GeoPackage path is chosen, start a new file each day (vatsim-history-YYYY-MM-DD.gpkg)
# instead of appending to one ever-growing file. Huge history tables make both writing and
# rendering slow.
ROTATE_DAILY = True

FIELDS = [
    QgsField("callsign", QMetaType.Type.QString, len=9),
    QgsField("cid", QMetaType.Type.Int),
    QgsField("heading", QMetaType.Type.Int),
    QgsField("groundspeed", QMetaType.Type.Int),
    QgsField("departure", QMetaType.Type.QString, len=4),
    QgsField("arrival", QMetaType.Type.QString, len=4),
    QgsField("aircraft", QMetaType.Type.QString, len=8),
    QgsField("timestamp", QMetaType.Type.QDateTime)
]


class HistoryLayer:
    """Where the history lives, and how to look at it. MAIN THREAD ONLY.

    While recording, the history GeoPackage is deliberately NOT loaded into QGIS: a layer that
    QGIS has open on the file being written can make the whole window wait whenever the recorder
    commits. Recording is done by HistoryWriter, which creates the GeoPackage and table itself.
    Call load_into_project() to look at the history once recording has stopped.
    """

    def __init__(self, gpkg_path: str = None, table_name=TABLE_NAME, pretty_name: str = PRETTY_NAME,
                 crs="EPSG:4326", svg_name: str = "aircraft.svg"):
        """
        :param gpkg_path: File path of the geopackage (a dated default is used if empty)
        :param table_name: Table in the GeoPackage
        :param pretty_name: Layer name used when the history is loaded into the project
        """
        if not gpkg_path or not gpkg_path.endswith('.gpkg'):
            folder = QgsProject.instance().homePath() or os.path.expanduser("~")
            file_name = f"vatsim-history-{datetime.now():%Y-%m-%d}.gpkg" if ROTATE_DAILY else "vatsim-history.gpkg"
            gpkg_path = os.path.join(folder, file_name)
            QgsMessageLog.logMessage(f"No geopackage path specified. Using default path: {gpkg_path}",
                                     level=Qgis.MessageLevel.Info)

        self.gpkg_path = gpkg_path
        self.table_name = table_name
        self.pretty_name = pretty_name
        self.crs = crs

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
        """Remove any layers loaded from this GeoPackage, so QGIS holds no connection to it.
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
        """Add the history table to the project so it can be viewed. Do this when NOT recording."""
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
    """Appends pilot positions to the history table using a plain OGR connection.

    open(), write() and close() must all be called from the SAME worker thread. It never
    touches a QgsVectorLayer, so it cannot block or race with the QGIS main thread.
    """

    in_process = True

    def set_logger(self, log, verbose: bool = False):
        pass

    def __init__(self, gpkg_path: str, table_name: str = TABLE_NAME,
                 stationary_max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M, record_every_n: int = 1):
        """
        :param stationary_max_groundspeed: aircraft at or below this speed (knots) are stationary
        :param move_tolerance_m: a stationary aircraft is only re-recorded if it moves this far
        :param record_every_n: only record every Nth snapshot (1 = every update, 2 = every
            other update, and so on) to thin the history further
        """
        self.gpkg_path = gpkg_path
        self.table_name = table_name
        self.filter = StationaryFilter(stationary_max_groundspeed, move_tolerance_m)
        self.last_skipped = 0  # unchanged stationary pilots skipped by the last write
        self.record_every_n = max(1, record_every_n)
        self._count = 0
        self.ds = None
        self.layer = None
        self.idx = {}
        self.last_timing = None  # (begin, insert loop, commit) in seconds, for debug logging

    def open(self):
        # WAL lets QGIS read the file while we write. Thread-local, so it doesn't change
        # anything for the rest of QGIS.
        if USE_WAL:
            gdal.SetThreadLocalConfigOption("OGR_SQLITE_JOURNAL", "WAL")
            if SQLITE_SYNCHRONOUS:
                gdal.SetThreadLocalConfigOption("OGR_SQLITE_SYNCHRONOUS", SQLITE_SYNCHRONOUS)
        self.ds, self.layer = open_history_layer(self.gpkg_path, self.table_name)
        defn = self.layer.GetLayerDefn()
        self.idx = {defn.GetFieldDefn(i).GetName(): i for i in range(defn.GetFieldCount())}

    def write(self, pilots: List[Pilot], timestamp: str) -> int:
        """Insert one snapshot in a single transaction. Returns the number of rows written."""
        self._count += 1
        if (self._count - 1) % self.record_every_n != 0:
            self.last_skipped = 0
            return 0

        defn = self.layer.GetLayerDefn()
        idx = self.idx
        written = 0
        skipped = 0
        recorded = {}  # callsign -> state, remembered only if the transaction commits

        t0 = time.perf_counter()
        self.layer.StartTransaction()
        t1 = time.perf_counter()
        try:
            for p in pilots:
                record, state = self.filter.check(p)
                if not record:
                    skipped += 1
                    continue
                recorded[p.callsign] = state
                feature = ogr.Feature(defn)
                point = ogr.Geometry(ogr.wkbPoint25D)
                point.AddPoint(p.longitude, p.latitude, p.altitude)
                feature.SetGeometry(point)
                feature.SetField(idx["callsign"], p.callsign)
                feature.SetField(idx["cid"], p.cid)
                feature.SetField(idx["heading"], p.heading)
                feature.SetField(idx["groundspeed"], p.groundspeed)
                feature.SetField(idx["departure"], p.departure)
                feature.SetField(idx["arrival"], p.arrival)
                feature.SetField(idx["aircraft"], p.aircraft)
                feature.SetField(idx["timestamp"], timestamp)
                self.layer.CreateFeature(feature)
                written += 1
            t2 = time.perf_counter()
            self.layer.CommitTransaction()
            self.last_timing = (t1 - t0, t2 - t1, time.perf_counter() - t2)
        except Exception:
            self.layer.RollbackTransaction()
            raise

        self.filter.commit(recorded, {p.callsign for p in pilots})
        self.last_skipped = skipped
        return written

    def close(self):
        self.layer = None
        self.ds = None  # closing the dataset flushes everything to disk
        if USE_WAL:
            gdal.SetThreadLocalConfigOption("OGR_SQLITE_JOURNAL", None)
            gdal.SetThreadLocalConfigOption("OGR_SQLITE_SYNCHRONOUS", None)


class HistoryWriter:
    """Records pilot positions to the history table without ever blocking QGIS.

    Normally the work is done by a separate process (history_recorder.py), so neither Python's
    lock nor the disk can hold up the QGIS window. If that process cannot be started, it falls
    back to InProcessHistoryWriter (a plain OGR connection inside the worker thread).

    open(), write() and close() must all be called from the same worker thread.
    """

    def __init__(self, gpkg_path: str, table_name: str = TABLE_NAME,
                 stationary_max_groundspeed: float = STATIONARY_MAX_GROUNDSPEED,
                 move_tolerance_m: float = STATIONARY_MOVE_TOLERANCE_M, record_every_n: int = 1):
        self.gpkg_path = gpkg_path
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
                self.gpkg_path, self.table_name, self.stationary_max_groundspeed,
                self.move_tolerance_m, self.record_every_n, USE_WAL, SQLITE_SYNCHRONOUS,
                log=self._log, verbose=self._verbose)
            try:
                client.open()
                self._impl = client
                self._say("History is being recorded by a separate process")
                return
            except RecorderUnavailable as e:
                self._say(f"Separate history recorder unavailable ({e}); recording inside QGIS instead")
        writer = InProcessHistoryWriter(self.gpkg_path, self.table_name, self.stationary_max_groundspeed,
                                        self.move_tolerance_m, self.record_every_n)
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
    def last_skipped(self) -> int:
        return self._impl.last_skipped if self.in_process else 0