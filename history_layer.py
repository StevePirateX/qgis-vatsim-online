import os

from qgis.PyQt.QtCore import QMetaType
from qgis._core import QgsVectorLayer, QgsProject, QgsFields, QgsField, QgsVectorFileWriter

from .vatsim_aircraft_layer import VatsimAircraftLayer

TABLE_NAME = 'vatsim_history'
PRETTY_NAME = "VATSIM History"

class HistoryLayer:
    def __init__(self, gpkg_path: str=None, table_name=TABLE_NAME, pretty_name: str=PRETTY_NAME, crs="EPSG:4326"):
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
            #self._apply_default_style()

        # Initialize feature tracking
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
        vatsim_aircraft_fields = VatsimAircraftLayer.FIELDS
        temp_layer.dataProvider().addAttributes(vatsim_aircraft_fields)
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



