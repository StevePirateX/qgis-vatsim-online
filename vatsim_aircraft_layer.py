from typing import List
import os

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QFont
from qgis._core import QgsSymbolLayer, QgsProperty, QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings, \
    QgsVectorLayerSimpleLabeling, QgsFields, QgsMessageLog, Qgis
from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPoint,
    QgsProject,
    QgsField,
    QgsMarkerSymbol,
    QgsSvgMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    edit
)
from qgis.PyQt.QtGui import QColor

from .vatsim_pilot import VatsimPilot


class VatsimAircraftLayer:
    def __init__(self, aircraft_colour: QColor, layer_name="VATSIM Data", crs="EPSG:4326", svg_name="aircraft.svg", ):
        """Initialize a memory layer for live point updates."""
        self.layer = QgsVectorLayer(f"Point?crs={crs}", layer_name, "memory")
        self.pr = self.layer.dataProvider()

        plugin_dir = os.path.dirname(__file__)  # Path to psx_vector_layer.py
        self.svg_path = os.path.join(plugin_dir, svg_name)

        # Define all attributes (expanded for VATSIM data)
        self.fields = [
            QgsField("callsign", QMetaType.Type.QString),
            QgsField("cid", QMetaType.Type.Int),
            QgsField("name", QMetaType.Type.QString),
            QgsField("latitude", QMetaType.Type.QString),
            QgsField("longitude", QMetaType.Type.QString),
            QgsField("altitude", QMetaType.Type.Int),
            QgsField("heading", QMetaType.Type.Int),
            QgsField("groundspeed", QMetaType.Type.Int),
            QgsField("transponder", QMetaType.Type.QString),
            QgsField("server", QMetaType.Type.QString),
            QgsField("departure", QMetaType.Type.QString),
            QgsField("arrival", QMetaType.Type.QString),
            QgsField("aircraft", QMetaType.Type.QString),
            QgsField("cruise", QMetaType.Type.QString),
            QgsField("route", QMetaType.Type.QString)
        ]
        self.qgs_fields = QgsFields()
        for field in self.fields:
            self.qgs_fields.append(field)

        self.pr.addAttributes(self.qgs_fields)
        self.layer.updateFields()

        QgsProject.instance().addMapLayer(self.layer)
        self.features = {}  # Dictionary to track features by CID
        self._apply_default_style(aircraft_colour=aircraft_colour)

    def _apply_default_style(self, aircraft_colour: QColor):
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
        text_format.setFont(QFont("Arial", 8, QFont.Bold))
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
        self.layer.setLabelsEnabled(True)

        # Force visibility and style sync
        tree_layer = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if tree_layer:
            tree_layer.setItemVisibilityChecked(True)

        # Mark layer as modified to force refresh
        self.layer.triggerRepaint()
        self.layer.reload()


    def full_refresh(self, pilots: List[VatsimPilot], hidden_vatsim_id: int=0):
        """
        Completely replace all features with fresh pilot data.

        Args:
            :param pilots: List of live VATSIM pilots
            :param hidden_vatsim_id: This allows the QGIS user to hide their own aircraft as they might be using another
                source. This is useful if they are using another plugin to display their own aircraft.
        """
        try:
            with edit(self.layer):
                # Remove all existing features
                self.pr.truncate()

                # Add fresh features for all current pilots
                features = []
                for pilot in pilots:
                    if pilot.cid == hidden_vatsim_id:
                        continue
                    pilot_feature = QgsFeature()
                    pilot_feature.setFields(self.qgs_fields)
                    pilot_feature.setGeometry(QgsGeometry.fromPoint(
                        QgsPoint(pilot.longitude, pilot.latitude, pilot.altitude)
                    ))

                    pilot_attributes = pilot.get_details()
                    for attribute, value in pilot_attributes.items():
                        field_index = pilot_feature.fields().indexFromName(attribute)
                        if field_index == -1:
                            continue
                        pilot_feature.setAttribute(field_index, value)

                    features.append(pilot_feature)

                self.pr.addFeatures(features)

            # Force a full refresh of the layer
            self.layer.triggerRepaint()
            self.layer.dataProvider().reloadData()
            return True

        except Exception as e:
            print(f"Error during full refresh: {e}")
            return False

    def rename_layer(self, name: str):
        self._apply_default_style(QColor(184,51,26))
        self.layer.setName(name)

    def remove_layer(self):
        """Remove the layer from QGIS."""
        QgsProject.instance().removeMapLayer(self.layer.id())