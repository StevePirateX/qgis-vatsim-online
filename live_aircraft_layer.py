import os
from typing import List

from qgis.PyQt.QtCore import QMetaType, QDateTime
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPoint,
    QgsProject,
    QgsField,
    QgsFields,
    QgsMarkerSymbol,
    QgsFontMarkerSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsProperty,
    QgsPalLayerSettings,
    QgsTextFormat,
    QgsTextBufferSettings,
    QgsVectorLayerSimpleLabeling,
)

from .pilot import Pilot

# IMPORTANT: the aircraft glyph is a private-use character from the Bravura font and
# may not have survived copy/paste. Paste the exact character from your original
# file's `font_layer.setCharacter("...")` call between the quotes below.
AIRCRAFT_GLYPH = ""


class VatsimAircraftLayer:
    FIELDS = [
        QgsField("callsign", QMetaType.Type.QString),
        QgsField("cid", QMetaType.Type.Int),
        QgsField("name", QMetaType.Type.QString),
        QgsField("latitude", QMetaType.Type.Double),
        QgsField("longitude", QMetaType.Type.Double),
        QgsField("altitude", QMetaType.Type.Int),
        QgsField("heading", QMetaType.Type.Int),
        QgsField("groundspeed", QMetaType.Type.Int),
        QgsField("transponder", QMetaType.Type.QString),
        QgsField("server", QMetaType.Type.QString),
        QgsField("departure", QMetaType.Type.QString),
        QgsField("arrival", QMetaType.Type.QString),
        QgsField("aircraft", QMetaType.Type.QString),
        QgsField("cruise", QMetaType.Type.QString),
        QgsField("route", QMetaType.Type.QString),
        QgsField("timestamp", QMetaType.Type.QDateTime),
    ]

    def __init__(self, aircraft_colour: QColor, layer_name="VATSIM Data", crs="EPSG:4326",
                 svg_name="aircraft.svg", save_history=False):
        """Initialize a memory layer for live point updates. MAIN THREAD ONLY."""
        self.layer = QgsVectorLayer(f"Point?crs={crs}", layer_name, "memory")
        self.pr = self.layer.dataProvider()

        plugin_dir = os.path.dirname(__file__)
        self.svg_path = os.path.join(plugin_dir, svg_name)

        self.qgs_fields = QgsFields()
        for field in VatsimAircraftLayer.FIELDS:
            self.qgs_fields.append(field)

        self.pr.addAttributes(self.qgs_fields)
        self.layer.updateFields()

        # Cached lookups so the refresh never has to iterate the layer
        self.field_names = [f.name() for f in self.layer.fields()]
        self.field_idx = {name: i for i, name in enumerate(self.field_names)}
        self.fid_by_callsign = {}  # callsign -> QGIS feature id

        QgsProject.instance().addMapLayer(self.layer)
        self._apply_default_style(aircraft_colour=aircraft_colour)

        self.save_history = save_history

    def _apply_default_style(self, aircraft_colour: QColor):
        """Set a visual style (arrow rotated by heading) and default label settings."""
        symbol = QgsMarkerSymbol()
        font_layer = QgsFontMarkerSymbolLayer()
        font_layer.setCharacter(AIRCRAFT_GLYPH)
        font_layer.setFontFamily("Bravura Text")
        font_layer.setSize(8)
        font_layer.setColor(aircraft_colour)
        font_layer.setStrokeColor(QColor('black'))
        font_layer.setStrokeWidth(0.2)
        symbol.changeSymbolLayer(0, font_layer)
        rotation_property = QgsProperty.fromExpression("heading - 45")
        symbol.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.Property.Angle,
            rotation_property
        )

        self.renderer = QgsSingleSymbolRenderer(symbol)
        self.layer.setRenderer(self.renderer)

        # Label styling
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "concat(callsign, '\\n', lpad(altitude//100,3,0), '   ', lpad(groundspeed//10,2,0))"
        label_settings.isExpression = True

        text_format = QgsTextFormat()
        text_format.setFont(QFont("Arial", 8, QFont.Weight.Bold))
        text_format.setColor(aircraft_colour)

        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(0.8)
        buffer_settings.setColor(QColor(0, 0, 0))
        text_format.setBuffer(buffer_settings)

        label_settings.setFormat(text_format)
        label_settings.dist = 5.0
        label_settings.quadOffset = QgsPalLayerSettings.QuadrantLeft

        self.layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        self.layer.setLabelsEnabled(False)

        # Force visibility
        tree_layer = QgsProject.instance().layerTreeRoot().findLayer(self.layer.id())
        if tree_layer:
            tree_layer.setItemVisibilityChecked(True)

        self.layer.triggerRepaint()

    @staticmethod
    def _values(pilot: Pilot, timestamp: QDateTime) -> dict:
        """Attribute values for one pilot, keyed by layer field name."""
        return dict(
            callsign=pilot.callsign,
            cid=pilot.cid,
            name=pilot.name,
            latitude=pilot.latitude,
            longitude=pilot.longitude,
            altitude=pilot.altitude,
            heading=pilot.heading,
            groundspeed=pilot.groundspeed,
            transponder=pilot.transponder,
            server=pilot.server,
            departure=pilot.departure,
            arrival=pilot.arrival,
            aircraft=pilot.aircraft,
            cruise=pilot.cruise,
            route=pilot.route,
            timestamp=timestamp,
        )

    @staticmethod
    def _geometry(pilot: Pilot) -> QgsGeometry:
        return QgsGeometry(QgsPoint(pilot.longitude, pilot.latitude, pilot.altitude))

    def apply_snapshot(self, pilots: List[Pilot], hidden_vatsim_id: int = 0) -> bool:
        """
        Sync the layer with the latest list of pilots. MAIN THREAD ONLY.

        Every provider operation is a single batched call, and the layer is only
        repainted once at the end.

        :param pilots: List of live VATSIM pilots
        :param hidden_vatsim_id: CID of the QGIS user's own aircraft to leave out (useful when
            another plugin/source is already displaying it).
        """
        try:
            timestamp = QDateTime.currentDateTime()
            current = {p.callsign: p for p in pilots if p.cid != hidden_vatsim_id}

            # 1. Remove pilots that have disconnected
            gone = self.fid_by_callsign.keys() - current.keys()
            if gone:
                self.pr.deleteFeatures([self.fid_by_callsign.pop(cs) for cs in gone])

            # 2. Update pilots that are already in the layer (two batched calls)
            geom_changes = {}
            attr_changes = {}
            for callsign, fid in self.fid_by_callsign.items():
                pilot = current[callsign]
                geom_changes[fid] = self._geometry(pilot)
                attr_changes[fid] = {
                    self.field_idx[name]: value
                    for name, value in self._values(pilot, timestamp).items()
                }
            if geom_changes:
                self.pr.changeGeometryValues(geom_changes)
                self.pr.changeAttributeValues(attr_changes)

            # 3. Add new pilots (one batched call)
            new_pilots = [p for cs, p in current.items() if cs not in self.fid_by_callsign]
            features = []
            for pilot in new_pilots:
                feature = QgsFeature(self.layer.fields())
                feature.setGeometry(self._geometry(pilot))
                values = self._values(pilot, timestamp)
                feature.setAttributes([values[name] for name in self.field_names])
                features.append(feature)

            if features:
                _, added = self.pr.addFeatures(features)
                for pilot, feature in zip(new_pilots, added):
                    self.fid_by_callsign[pilot.callsign] = feature.id()

            print(f"VATSIM Pilot Refresh: Updated: {len(geom_changes)}; "
                  f"New: {len(features)}; Deleted: {len(gone)}")

            self.layer.triggerRepaint()
            return True

        except Exception as e:
            print(f"Error during refresh: {e}")
            return False

    def rename_layer(self, name: str):
        self._apply_default_style(QColor(184, 51, 26))
        self.layer.setName(name)

    def remove_layer(self):
        """Remove the layer from QGIS."""
        QgsProject.instance().removeMapLayer(self.layer.id())