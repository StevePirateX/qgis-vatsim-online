"""File naming for the daily history files, shared by the QGIS plugin, the recorder process and
the standalone worker.

Pure Python (no QGIS or GDAL).

One GeoPackage is written per day:   <folder>/<base name>-YYYY-MM-DD.gpkg
When the day is over it is converted to <folder>/<base name>-YYYY-MM-DD.parquet
"""
import os
import re
from datetime import date, datetime, timezone

DEFAULT_BASE_NAME = "vatsim-history"

# False: a day ends at local midnight. True: a day ends at midnight UTC (the time zone VATSIM's
# own timestamps use).
HISTORY_DAY_USES_UTC = False

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def today_date() -> date:
    """The current date, in the time zone chosen by HISTORY_DAY_USES_UTC."""
    return datetime.now(timezone.utc).date() if HISTORY_DAY_USES_UTC else datetime.now().date()


def resolve_base(path: str, default_folder: str):
    """Work out (folder, base name) from whatever the user entered.

    * "/data/vatsim-history.gpkg"            -> ("/data", "vatsim-history")
    * "/data/vatsim-history-2026-10-25.gpkg" -> ("/data", "vatsim-history")  (date part dropped)
    * "/data"  (an existing folder)          -> ("/data", DEFAULT_BASE_NAME)
    * ""                                     -> (default_folder, DEFAULT_BASE_NAME)
    """
    path = (path or "").strip()
    if path.lower().endswith(".gpkg"):
        folder = os.path.dirname(path) or default_folder
        base = _DATE_SUFFIX.sub("", os.path.splitext(os.path.basename(path))[0])
        return folder, base or DEFAULT_BASE_NAME
    if path and os.path.isdir(path):
        return path, DEFAULT_BASE_NAME
    return default_folder, DEFAULT_BASE_NAME


def daily_gpkg_path(folder: str, base_name: str, day: date) -> str:
    return os.path.join(folder, f"{base_name}-{day:%Y-%m-%d}.gpkg")


def parquet_path_for(gpkg_path: str) -> str:
    return os.path.splitext(gpkg_path)[0] + ".parquet"


def finished_gpkgs(folder: str, base_name: str, today: date) -> list:
    """Daily GeoPackages in the folder for days BEFORE today (oldest first)."""
    pattern = re.compile(rf"^{re.escape(base_name)}-(\d{{4}})-(\d{{2}})-(\d{{2}})\.gpkg$")
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        match = pattern.match(name)
        if not match:
            continue
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if day < today:
            found.append((day, os.path.join(folder, name)))
    return [path for _, path in sorted(found)]