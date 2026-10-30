"""Opening (and if necessary creating) the history table.

Uses GDAL/OGR only, never QGIS, so it can run inside the separate recorder process. That means
QGIS never needs to touch the history GeoPackage while recording.
"""
import os

from osgeo import ogr, osr

LAST_SEEN = "last_seen"

# (name, OGR type, width). `timestamp` is when a row starts and `last_seen` the last time the
# aircraft was still observed in that state. They are equal for a moving aircraft; for a
# stationary one `last_seen` is pushed forward while nothing changes, so last_seen - timestamp
# is how long it stayed there.
FIELDS = [
    ("callsign", ogr.OFTString, 9),
    ("cid", ogr.OFTInteger, 0),
    ("heading", ogr.OFTInteger, 0),
    ("groundspeed", ogr.OFTInteger, 0),
    ("departure", ogr.OFTString, 4),
    ("arrival", ogr.OFTString, 4),
    ("aircraft", ogr.OFTString, 8),
    ("timestamp", ogr.OFTDateTime, 0),
    (LAST_SEEN, ogr.OFTDateTime, 0),
]


def open_history_layer(gpkg_path: str, table_name: str):
    """Open the history table for writing, creating the GeoPackage and/or the table if needed,
    and adding any columns an older table is missing (such as last_seen).

    Returns (dataset, layer). Keep a reference to the dataset for as long as the layer is used.
    """
    if os.path.exists(gpkg_path) and os.path.getsize(gpkg_path) == 0:
        os.remove(gpkg_path)  # an empty file is left over from a failed create
    ds = ogr.Open(gpkg_path, update=1) if os.path.exists(gpkg_path) else None
    if ds is None:
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg_path)
        if ds is None:
            raise RuntimeError(f"could not create the GeoPackage {gpkg_path}")

    layer = ds.GetLayerByName(table_name)
    if layer is None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        if hasattr(osr, "OAMS_TRADITIONAL_GIS_ORDER"):
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)  # x = longitude
        layer = ds.CreateLayer(table_name, srs, ogr.wkbPoint25D)
        if layer is None:
            raise RuntimeError(f"could not create the table '{table_name}'")

    defn = layer.GetLayerDefn()
    for name, ogr_type, width in FIELDS:
        if defn.GetFieldIndex(name) < 0:
            field = ogr.FieldDefn(name, ogr_type)
            if width:
                field.SetWidth(width)
            layer.CreateField(field)
    return ds, layer