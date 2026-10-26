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
    QgsUnitTypes,
    QgsWkbTypes,
)

# IMPORTANT: the aircraft glyph is a private-use character from the Bravura font and
# may not have survived copy/paste. Paste the exact character from your original
# file's `font_layer.setCharacter("...")` call between the quotes below.
AIRCRAFT_GLYPH = ""

LAYER_NAME = "VATSIM Data"


class LayerUnusable(Exception):
    """An existing "VATSIM Data" layer was found but data can't be written into it."""


class VatsimAircraftLayer:
    """The live "VATSIM Data" layer.

    The layer is permanent: it stays in the project when updates stop (showing the last data)
    and is reused, with its symbology untouched, the next time updates start. Only if the
    project has no point layer called "VATSIM Data" is a new scratch layer created, with the
    default style.

    MAIN THREAD ONLY, except for prepare_snapshot(), which is safe to call from a worker thread.
    """

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
    FIELD_NAMES = [f.name() for f in FIELDS]

    # What kind of value each field holds, used to decide whether a field in an existing layer
    # can take it: "string" needs a text field, "numeric" a number (or text), "datetime" a
    # date/time (or text).
    FIELD_KINDS = {
        "callsign": "string", "cid": "numeric", "name": "string", "latitude": "numeric",
        "longitude": "numeric", "altitude": "numeric", "heading": "numeric",
        "groundspeed": "numeric", "transponder": "string", "server": "string",
        "departure": "string", "arrival": "string", "aircraft": "string", "cruise": "string",
        "route": "string", "timestamp": "datetime",
    }

    # Updated on every refresh
    DYNAMIC_FIELDS = ("latitude", "longitude", "altitude", "heading", "groundspeed",
                      "transponder", "timestamp")
    # Only rewritten when they actually change (the route strings are long)
    FLIGHTPLAN_FIELDS = ("departure", "arrival", "aircraft", "cruise", "route")

    # ------------------------------------------------------------------ finding / creating

    @staticmethod
    def find_existing_layer(layer_name: str = LAYER_NAME):
        """The first POINT layer in the project with this name, or None. Layers with the same
        name that are not (single) point layers are ignored."""
        for layer in QgsProject.instance().mapLayersByName(layer_name):
            if (isinstance(layer, QgsVectorLayer) and layer.isValid()
                    and layer.geometryType() == Qgis.GeometryType.Point
                    and not QgsWkbTypes.isMultiType(layer.wkbType())):
                return layer
        return None

    @classmethod
    def acquire(cls, aircraft_colour: QColor, layer_name: str = LAYER_NAME):
        """Use the project's existing "VATSIM Data" point layer, or create one.

        :raises LayerUnusable: an existing layer was found but can't be written to
        """
        layer = cls.find_existing_layer(layer_name)
        if layer is None:
            return cls._create(aircraft_colour, layer_name)
        cls._check_writable(layer)
        return cls(layer)

    @staticmethod
    def _check_writable(layer: QgsVectorLayer):
        if layer.isEditable():
            raise LayerUnusable(f"layer '{layer.name()}' is in edit mode; turn editing off and try again")
        caps = layer.dataProvider().capabilities()
        needed = {
            "add features": Qgis.VectorProviderCapability.AddFeatures,
            "delete features": Qgis.VectorProviderCapability.DeleteFeatures,
            "change attribute values": Qgis.VectorProviderCapability.ChangeAttributeValues,
            "change geometries": Qgis.VectorProviderCapability.ChangeGeometries,
        }
        missing = [what for what, flag in needed.items() if not (caps & flag)]
        if missing:
            raise LayerUnusable(f"layer '{layer.name()}' can't {', '.join(missing)} (is it read-only?)")

    @classmethod
    def _create(cls, aircraft_colour: QColor, layer_name: str, crs: str = "EPSG:4326"):
        layer = QgsVectorLayer(f"Point?crs={crs}", layer_name, "memory")
        qgs_fields = QgsFields()
        for field in cls.FIELDS:
            qgs_fields.append(field)
        layer.dataProvider().addAttributes(qgs_fields)
        layer.updateFields()
        QgsProject.instance().addMapLayer(layer)
        wrapper = cls(layer, created=True)
        wrapper._apply_default_style(aircraft_colour)
        return wrapper

    def __init__(self, layer: QgsVectorLayer, created: bool = False):
        self.layer = layer
        self.pr = layer.dataProvider()
        self.created = created
        self.fid_by_callsign = {}  # callsign -> QGIS feature id
        self._fp_sig = {}          # callsign -> last written flight plan values
        self.field_idx, self.skipped_fields = self._map_fields(layer)
        self.field_count = layer.fields().count()
        # An existing layer keeps its old data until the first fresh snapshot replaces it
        self._needs_clear = (not created) and layer.featureCount() > 0

    # ------------------------------------------------------------------ field matching

    @classmethod
    def _map_fields(cls, layer: QgsVectorLayer):
        """Match our fields to the layer's fields by NAME.

        Returns ({field name: index in the layer}, {field name: reason it was skipped}).
        Fields the layer doesn't have, or has with an unsuitable type, are skipped, so nothing
        is ever inserted into the wrong place; extra fields in the layer are left empty.
        """
        layer_fields = {f.name(): (i, f) for i, f in enumerate(layer.fields())}
        mapped, skipped = {}, {}
        for name in cls.FIELD_NAMES:
            found = layer_fields.get(name)
            if found is None:
                skipped[name] = "not in the layer"
            elif not cls._compatible(cls.FIELD_KINDS[name], found[1]):
                skipped[name] = "unsuitable field type"
            else:
                mapped[name] = found[0]
        return mapped, skipped

    @staticmethod
    def _compatible(kind: str, field) -> bool:
        if kind == "string":
            return not field.isNumeric() and not field.isDateOrTime()
        if kind == "numeric":
            return not field.isDateOrTime()
        return field.isDateOrTime() or not field.isNumeric()  # datetime

    def describe(self) -> str:
        """One line for the log saying which layer is being used and how well it matches."""
        kind = "Created new" if self.created else "Using existing"
        text = (f"{kind} layer '{self.layer.name()}': {len(self.field_idx)} of "
                f"{len(self.FIELD_NAMES)} VATSIM fields match")
        if self.skipped_fields:
            text += "; skipped " + ", ".join(f"{n} ({why})" for n, why in self.skipped_fields.items())
        return text

    def is_alive(self) -> bool:
        """False if the layer has been deleted from QGIS since updates started."""
        try:
            self.layer.id()
            return True
        except RuntimeError:
            return False

    # ------------------------------------------------------------------ styling (new layers only)

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

    # ------------------------------------------------------------------ data

    @staticmethod
    def prepare_snapshot(pilots, hidden_vatsim_id: int, field_idx: dict, field_count: int) -> dict:
        """
        Build everything the layer needs from a list of pilots, WITHOUT touching a layer.

        Safe to call from the worker thread: it only creates plain Python data plus
        standalone QgsGeometry / QDateTime value objects. Doing this work off the main
        thread leaves apply_snapshot() with almost nothing to do.

        :param hidden_vatsim_id: CID of the QGIS user's own aircraft to leave out (useful
            when another plugin/source is already displaying it).
        :param field_idx: {our field name: index in the target layer}, from the layer wrapper
        :param field_count: number of fields in the target layer
        :return: {callsign: (geometry, dynamic_attrs, flightplan_sig, flightplan_attrs, all_values)}
        """
        cls = VatsimAircraftLayer
        dynamic_names = [n for n in cls.DYNAMIC_FIELDS if n in field_idx]
        flightplan_names = [n for n in cls.FLIGHTPLAN_FIELDS if n in field_idx]
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
            all_values = [None] * field_count
            for name, index in field_idx.items():
                all_values[index] = values[name]
            prepared[p.callsign] = (
                QgsGeometry(QgsPoint(p.longitude, p.latitude, p.altitude)),
                {field_idx[n]: values[n] for n in dynamic_names},
                tuple(values[n] for n in flightplan_names),
                {field_idx[n]: values[n] for n in flightplan_names},
                all_values,
            )
        return prepared

    def _clear_layer(self):
        """Remove every feature (used when updates start again, to replace the old data)."""
        if not self.pr.truncate():
            self.pr.deleteFeatures(self.pr.allFeatureIds())
        self.fid_by_callsign.clear()
        self._fp_sig.clear()

    def apply_snapshot(self, prepared: dict) -> bool:
        """
        Sync the layer with a snapshot from prepare_snapshot(). MAIN THREAD ONLY.

        Every provider operation is a single batched call and the layer is repainted
        once at the end. The first snapshot after updates (re)start replaces whatever the
        layer held before.
        """
        try:
            if self._needs_clear:
                self._clear_layer()
                self._needs_clear = False

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

    def remove_layer(self):
        """Remove the layer from the project (not used by the plugin; the layer is permanent)."""
        QgsProject.instance().removeMapLayer(self.layer.id())