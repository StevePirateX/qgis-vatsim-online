# VATSIM Online QGIS Plugin

This plugin enables the user to be able to fetch live VATSIM data via the [VATSIM Data API](https://vatsim.dev/api/data-api/vatsim-data-api) and inject it into QGIS for viewing. It will refresh the data at a specified rate and update the layer so the map is current. The style is automatically applied to the layer with aircraft SVGs so it, by default, looks similar to the `map.vatsim.net` service. It also allows for the ability for the data to be recorded into a GeoPackage so the data can be played back using the Temporal Controller. To reduce file sizes, only necessary data is recorded in the history. This includes:

- Callsign
- VATSIM ID
- Position with altitude in the geometry as x, y and z
- Heading
- Groundspeed
- Departure ICAO
- Arrival ICAO
- Aircraft Type
- Timestamp recorded by the VATSIM data file

The departure, arrival and aircraft type allows for filtering when reviewing the history. Fields such as name, server connect to, and route are deemed not beneficial for historic analysis.

The plugin has been tested on QGIS 3.44. It has NOT been converted to QGIS 4 yet.

## Standalone Script

The plugin also uses a `standalone_history_worker.py` to be able to record the data to a GeoPackage using a terminal. This enables the data to be recorded without using the Desktop QGIS.

To use the standalone script, a `.env` file must be created with the following parameters defined:

- VATSIM_DATA_URL: This is the JSON URL for the VATSIM API. Currently it is `https://data.vatsim.net/v3/vatsim-data.json`
- REFRESH_TIME_SECS: The number of seconds between refreshes. The VATSIM data file refreshes every 15 seconds and so this is usually set to `15`
- GPKG_PATH: The file location of the GeoPackage. e.g. `/home/user/vatsim/history.gpkg`
- TABLE_NAME: Name of the table inside the GeoPackage that will contain the data. e.g. `vatsim_history`

The script can then just be run with `python3 standalone_history_worker.py`
