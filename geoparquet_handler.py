import geoparquet_io as gpio

def convert_to_geoparquet(input_file: str, output_file: str):
    """Convert the input gpkg to geoparquet.

    :param
    input_file: input geopackage file
    output_file: output geoparquet"""
    table = gpio.convert(input_file)
    table.write(output_file)
    print(f"{table.num_rows:,} rows converted to GeoParquet")


INPUT_FILE = 'data_output/vatsim-history.gpkg'
OUTPUT_FILE = 'data_output/vatsim-history.parquet'
convert_to_geoparquet(INPUT_FILE, OUTPUT_FILE)