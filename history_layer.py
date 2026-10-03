import os
from typing import List

from qgis.PyQt.QtCore import QDateTime, QMetaType
from qgis.PyQt.QtGui import QColor, QFont
from qgis._core import QgsVectorLayer, QgsProject, QgsFields, QgsField, QgsVectorFileWriter, QgsMarkerSymbol, \
    QgsSvgMarkerSymbolLayer, QgsSymbolLayer, QgsProperty, QgsSingleSymbolRenderer, QgsPalLayerSettings, QgsTextFormat, \
    QgsTextBufferSettings, QgsVectorLayerSimpleLabeling, QgsFeature, QgsGeometry, QgsPoint
from qgis.core import edit

from .pilot import Pilot
from .live_aircraft_layer import VatsimAircraftLayer

TABLE_NAME = 'vatsim_history'
PRETTY_NAME = "VATSIM History"

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

    def __init__(self, gpkg_path: str=None, table_name=TABLE_NAME, pretty_name: str=PRETTY_NAME, crs="EPSG:4326", svg_name: str="aircraft.svg"):
        """
        Initialise the history layer

        :param gpkg_path: File path of the geopackage
        :param table_name: Table in the GeoPackage
        :param pretty_name: Layer name that will be displayed in the layer panel
        :param crs: CRS of the layer
        """
        if gpkg_path is None:
            project_path = QgsProject.instance().homePath()
            gpkg_path = os.path.join(project_path, 'History.gpkg')

        self.gpkg_path = gpkg_path
        self.table_name = table_name
        self.pretty_name = pretty_name
        self.crs = crs

        plugin_dir = os.path.dirname(__file__)  # Path to psx_vector_layer.py
        self.svg_path = os.path.join(plugin_dir, svg_name)

        # Ensure .gpkg extension
        if not self.gpkg_path.lower().endswith('.gpkg'):
           self.gpkg_path += '.gpkg'

        # First check if layer already exists in project
        existing_layer = HistoryLayer.find_existing_layer(path=gpkg_path)

        if existing_layer:
            self.layer = existing_layer
            print(f"Using existing layer: {self.layer.name()}")
        else:
            self.layer = self._load_or_create_layer(crs)
            QgsProject.instance().addMapLayer(self.layer)
            self._apply_default_style()

        # Initialize feature tracking
        self.pr = self.layer.dataProvider()
        self.current_feature = None
        self.points = []


    @staticmethod
    def find_existing_layer(path: str="", project: QgsProject=QgsProject.instance()) -> QgsVectorLayer|None:
        """Check if layer already exists in project."""
        for layer in project.mapLayers().values():
            if (isinstance(layer, QgsVectorLayer) and
                    ((layer.dataProvider().dataSourceUri().startswith(path) or len(path) == 0) and
                     layer.name() == PRETTY_NAME)):
                return layer
        return None


    def _load_or_create_layer(self, crs: str) -> QgsVectorLayer:
        """Load from GeoPackage or create new layer if needed."""
        uri = f"{self.gpkg_path}|layername={self.table_name}"
        layer = QgsVectorLayer(uri, self.pretty_name, "ogr")

        if not layer.isValid():
            layer = self._create_layer_in_geopackage(crs)

        return layer

    def _create_layer_in_geopackage(self, crs: str) -> QgsVectorLayer:
        """Create a new LineStringZ layer in the GeoPackage."""
        # Create a temporary memory layer with correct schema
        temp_layer = QgsVectorLayer(f"PointZ?crs={crs}", "temp", "memory")
        temp_layer.startEditing()

        # Add fields
        history_fields = FIELDS
        temp_layer.dataProvider().addAttributes(history_fields)
        temp_layer.updateFields()

        # Save to GeoPackage
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = self.table_name
        # options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer

        error = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer=temp_layer,
            fileName=self.gpkg_path,
            transformContext=QgsProject.instance().transformContext(),
            options=options
        )

        if error[0] != QgsVectorFileWriter.NoError:
            raise RuntimeError(f"Failed to create GeoPackage layer: {error[1]}")

        return QgsVectorLayer(f"{self.gpkg_path}|layername={self.table_name}", self.pretty_name, "ogr")

    def _apply_default_style(self, aircraft_colour: QColor=QColor(0,255,255)):
        """Set a visual style (blue arrow for heading)."""
        symbol = QgsMarkerSymbol()
        svg_layer = QgsSvgMarkerSymbolLayer(self.svg_path)
        svg_layer.setSize(8)
        svg_layer.setColor(aircraft_colour)
        symbol.changeSymbolLayer(0, svg_layer)
        rotation_property = QgsProperty.fromExpression("heading + 90")
        symbol.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.Property.Angle,
            rotation_property
        )

        self.renderer = QgsSingleSymbolRenderer(symbol)
        self.layer.setRenderer(self.renderer)

        """Configure default label styling for the layer."""
        label_settings = QgsPalLayerSettings()

        # 1. Label content (using 'id' and 'speed' fields)
        label_settings.fieldName = "concat(callsign, \'\\n\', lpad(altitude//100,3,0), \'   \', lpad(groundspeed//10,2,0))"
        label_settings.isExpression = True

        # 2. Text formatting
        text_format = QgsTextFormat()
        text_format.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        text_format.setColor(aircraft_colour)

        # Add text buffer (black outline)
        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(0.8)
        buffer_settings.setColor(QColor(0, 0, 0))
        text_format.setBuffer(buffer_settings)

        label_settings.setFormat(text_format)

        # 3. Label placement
        # label_settings.placement = QgsPalLayerSettings.AroundPoint
        label_settings.dist = 5.0  # 2mm offset from point
        label_settings.quadOffset = QgsPalLayerSettings.QuadrantLeft  # Left side

        # Apply labeling
        self.layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        self.layer.setLabelsEnabled(False)

        # Force visibility and style sync
        tree_layer = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if tree_layer:
            tree_layer.setItemVisibilityChecked(False)

        # Mark layer as modified to force refresh
        self.layer.triggerRepaint()
        self.layer.reload()

    def add_pilot_positions(self, pilots: List[Pilot], timestamp):
        try:
            with edit(self.layer):

                features = []
                for pilot in pilots:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPoint(
                        QgsPoint(pilot.longitude, pilot.latitude, pilot.altitude)
                    ))
                    # Set basic attributes (no flight plan data)
                    feat.setAttributes([
                        None,
                        pilot.callsign,
                        pilot.cid,
                        pilot.heading,
                        pilot.groundspeed,
                        pilot.departure,
                        pilot.arrival,
                        pilot.aircraft,
                        timestamp
                    ])
                    features.append(feat)

                self.pr.addFeatures(features)
            self.layer.triggerRepaint()
            #self.layer.dataProvider().reloadData()

            return True

        except Exception as e:
            print(f"Error during full refresh: {e}")
            return False

    def remove_layer(self):
        """Remove the layer from QGIS."""
        QgsProject.instance().removeMapLayer(self.layer.id())

    def create_spatial_index(self):
        if self.layer.isEditable():
            self.layer.commitChanges()

        # Create the spatial index via the data provider
        if not self.layer.dataProvider().hasSpatialIndex():
            self.layer.dataProvider().createSpatialIndex()

        self.layer.triggerRepaint()



