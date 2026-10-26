import os

from qgis.PyQt.QtCore import QMetaType, QDateTime, QPointF
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
    QgsMessageLog,
    Qgis,
    QgsUnitTypes
)

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

    # Field order/indices are fixed by FIELDS, so the worker thread can use them without
    # ever touching a layer.
    FIELD_NAMES = [f.name() for f in FIELDS]
    FIELD_IDX = {name: i for i, name in enumerate(FIELD_NAMES)}

    # Updated on every refresh
    DYNAMIC_FIELDS = ("latitude", "longitude", "altitude", "heading", "groundspeed",
                      "transponder", "timestamp")
    # Only rewritten when they actually change (the route strings are long)
    FLIGHTPLAN_FIELDS = ("departure", "arrival", "aircraft", "cruise", "route")

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

        self.fid_by_callsign = {}  # callsign -> QGIS feature id
        self._fp_sig = {}          # callsign -> last written flight plan values

        QgsProject.instance().addMapLayer(self.layer)
        self._apply_default_style(aircraft_colour=aircraft_colour)

        self.save_history = save_history

    def _apply_default_style(self, aircraft_colour: QColor):
        """Set a visual style (arrow rotated by heading) and default label settings."""
        if AIRCRAFT_GLYPH:
            symbol = QgsMarkerSymbol()
            font_layer = QgsFontMarkerSymbolLayer()
            font_layer.setCharacter(AIRCRAFT_GLYPH)
            font_layer.setFontFamily("Bravura Text")
            font_layer.setSize(6)
            font_layer.setColor(aircraft_colour)
            font_layer.setStrokeColor(QColor('black'))
            font_layer.setStrokeWidth(0.2)
            font_layer.setOffset(QPointF(-0.6, -1.0))
            font_layer.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
            symbol.changeSymbolLayer(0, font_layer)
            rotation_property = QgsProperty.fromExpression("heading - 45")
            symbol.symbolLayer(0).setDataDefinedProperty(
                QgsSymbolLayer.Property.Angle,
                rotation_property
            )
        else:
            # No glyph set: an empty font marker draws nothing, so fall back to a visible arrow
            symbol = QgsMarkerSymbol.createSimple({
                'name': 'arrow', 'size': '5',
                'color': aircraft_colour.name(), 'outline_color': 'black',
            })
            symbol.setDataDefinedAngle(QgsProperty.fromExpression("heading"))

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
    def prepare_snapshot(pilots, hidden_vatsim_id: int = 0) -> dict:
        """
        Build everything the layer needs from a list of pilots, WITHOUT touching a layer.

        Safe to call from the worker thread: it only creates plain Python data plus
        standalone QgsGeometry / QDateTime value objects. Doing this work off the main
        thread leaves apply_snapshot() with almost nothing to do.

        :param hidden_vatsim_id: CID of the QGIS user's own aircraft to leave out (useful
            when another plugin/source is already displaying it).
        :return: {callsign: (geometry, dynamic_attrs, flightplan_sig, flightplan_attrs, all_values)}
        """
        cls = VatsimAircraftLayer
        idx = cls.FIELD_IDX
        timestamp = QDateTime.currentDateTime()
        prepared = {}
        for p in pilots:
            if p.cid == hidden_vatsim_id:
                continue
            values = dict(
                callsign=p.callsign, cid=p.cid, name=p.name,
                latitude=p.latitude, longitude=p.longitude, altitude=p.altitude,
                heading=p.heading, groundspeed=p.groundspeed,
                transponder=p.transponder, server=p.server,
                departure=p.departure, arrival=p.arrival, aircraft=p.aircraft,
                cruise=p.cruise, route=p.route, timestamp=timestamp,
            )
            prepared[p.callsign] = (
                QgsGeometry(QgsPoint(p.longitude, p.latitude, p.altitude)),
                {idx[n]: values[n] for n in cls.DYNAMIC_FIELDS},
                tuple(values[n] for n in cls.FLIGHTPLAN_FIELDS),
                {idx[n]: values[n] for n in cls.FLIGHTPLAN_FIELDS},
                [values[n] for n in cls.FIELD_NAMES],
            )
        return prepared

    def apply_snapshot(self, prepared: dict) -> bool:
        """
        Sync the layer with a snapshot from prepare_snapshot(). MAIN THREAD ONLY.

        Every provider operation is a single batched call and the layer is repainted
        once at the end.
        """
        try:
            # 1. Remove pilots that have disconnected
            gone = self.fid_by_callsign.keys() - prepared.keys()
            if gone:
                self.pr.deleteFeatures([self.fid_by_callsign.pop(cs) for cs in gone])
                for cs in gone:
                    self._fp_sig.pop(cs, None)

            # 2. Update pilots already in the layer (two batched calls)
            geom_changes = {}
            attr_changes = {}
            fp_sig_store = self._fp_sig
            for callsign, fid in self.fid_by_callsign.items():
                geom, dynamic, fp_sig, fp_attrs, _ = prepared[callsign]
                geom_changes[fid] = geom
                if fp_sig != fp_sig_store.get(callsign):
                    fp_sig_store[callsign] = fp_sig
                    attr_changes[fid] = {**dynamic, **fp_attrs}
                else:
                    attr_changes[fid] = dynamic
            if geom_changes:
                self.pr.changeGeometryValues(geom_changes)
                self.pr.changeAttributeValues(attr_changes)

            # 3. Add new pilots (one batched call)
            new_callsigns = [cs for cs in prepared if cs not in self.fid_by_callsign]
            features = []
            for callsign in new_callsigns:
                geom, _, _, _, all_values = prepared[callsign]
                feature = QgsFeature(self.layer.fields())
                feature.setGeometry(geom)
                feature.setAttributes(all_values)
                features.append(feature)

            if features:
                _, added = self.pr.addFeatures(features)
                for callsign, feature in zip(new_callsigns, added):
                    self.fid_by_callsign[callsign] = feature.id()
                    fp_sig_store[callsign] = prepared[callsign][2]

            print(f"VATSIM Pilot Refresh: Updated: {len(geom_changes)}; "
                  f"New: {len(features)}; Deleted: {len(gone)}")

            self.layer.triggerRepaint()
            return True

        except Exception as e:
            print(f"Error during refresh: {e}")
            QgsMessageLog.logMessage(f"Error during refresh: {e}", level=Qgis.MessageLevel.Critical)
            return False

    def rename_layer(self, name: str):
        self._apply_default_style(QColor(184, 51, 26))
        self.layer.setName(name)

    def remove_layer(self):
        """Remove the layer from QGIS."""
        QgsProject.instance().removeMapLayer(self.layer.id())