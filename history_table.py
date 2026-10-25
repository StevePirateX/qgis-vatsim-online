"""Opening (and if necessary creating) the history table.

Uses GDAL/OGR only, never QGIS, so it can run inside the separate recorder process. That means
QGIS never needs to touch the history GeoPackage while recording.
"""
import os

from osgeo import ogr, osr

# (name, OGR type, width) - the same columns the plugin has always recorded
FIELDS = [
    ("callsign", ogr.OFTString, 9),
    ("cid", ogr.OFTInteger, 0),
    ("heading", ogr.OFTInteger, 0),
    ("groundspeed", ogr.OFTInteger, 0),
    ("departure", ogr.OFTString, 4),
    ("arrival", ogr.OFTString, 4),
    ("aircraft", ogr.OFTString, 8),
    ("timestamp", ogr.OFTDateTime, 0),
]


def open_history_layer(gpkg_path: str, table_name: str):
    """Open the history table for writing, creating the GeoPackage and/or the table if needed.

    Returns (dataset, layer). Keep a reference to the dataset for as long as the layer is used.
    """
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
        for name, ogr_type, width in FIELDS:
            field = ogr.FieldDefn(name, ogr_type)
            if width:
                field.SetWidth(width)
            layer.CreateField(field)
    return ds, layer