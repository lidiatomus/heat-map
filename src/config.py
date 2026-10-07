from pathlib import Path

# Root project directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Data folders
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
DEM_PATH = DATA_RAW / "dem" / "zolder_dem.tif"

# Output folders
OUTPUT_PLOTS = PROJECT_ROOT / "outputs" / "plots"

# Files
TRACK_OSM_GEOJSON = DATA_PROCESSED / "zolder_track_osm.geojson"
TRACK_3D_CSV = DATA_PROCESSED / "zolder_track_3d.csv"
TRACK_3D_GEOJSON = DATA_PROCESSED / "zolder_track_3d.geojson"

# Approximate Circuit Zolder area
CENTER_LAT = 50.9898
CENTER_LON = 5.2569

# Bounding box around the track
# north, south, east, west
BBOX_NORTH = 50.9945
BBOX_SOUTH = 50.9855
BBOX_EAST = 5.2655
BBOX_WEST = 5.2485

# Geometry settings
DENSIFY_STEP_METERS = 3.0

# CRSs
WGS84 = "EPSG:4326"
METRIC_CRS = "EPSG:3857"

TRACKS = {
    "zolder": {
        "name": "Zolder",
        "lat": 50.9898,
        "lon": 5.2569,
        "timezone": "Europe/Brussels",
        "track_osm_geojson": DATA_PROCESSED / "zolder_track_osm.geojson",
        "track_3d_csv": DATA_PROCESSED / "zolder_track_3d.csv",
        "track_3d_geojson": DATA_PROCESSED / "zolder_track_3d.geojson",
        "obstacles_geojson": DATA_PROCESSED / "zolder_obstacles.geojson",
        "output_prefix": "zolder",
        "dem_path": DATA_RAW / "dem" / "zolder_dem.tif",
    },
    "bucharest": {
        "name": "Bucharest",
        "lat": 44.4268,
        "lon": 26.1025,
        "timezone": "Europe/Bucharest",
        "track_osm_geojson": DATA_PROCESSED / "bucharest_track_osm.geojson",
        "track_3d_csv": DATA_PROCESSED / "bucharest_track_3d.csv",
        "track_3d_geojson": DATA_PROCESSED / "bucharest_track_3d.geojson",
        "obstacles_geojson": DATA_PROCESSED / "bucharest_obstacles.geojson",
        "output_prefix": "bucharest",
        "dem_path": DATA_RAW / "dem" / "bucharest_dem.tif",

    },
    "targu_mures": {
        "name": "Transilvania Motor Ring",
        "lat": 46.43528,
        "lon": 24.42694,
        "timezone": "Europe/Bucharest",
        "track_osm_geojson": DATA_PROCESSED / "targu_mures_track_osm.geojson",
        "track_3d_csv": DATA_PROCESSED / "targu_mures_track_3d.csv",
        "track_3d_geojson": DATA_PROCESSED / "targu_mures_track_3d.geojson",
        "obstacles_geojson": DATA_PROCESSED / "targu_mures_obstacles.geojson",
        "output_prefix": "targu_mures",
        "dem_path": DATA_RAW / "dem" / "targu_mures_dem.tif",
    },
}
