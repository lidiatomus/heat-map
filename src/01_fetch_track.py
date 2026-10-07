from __future__ import annotations

import argparse
import sys

import geopandas as gpd
import osmnx as ox

from config import DATA_PROCESSED, TRACKS, WGS84
from utils_geo import ensure_single_linestring


def main() -> None:
    parser = argparse.ArgumentParser(description="Download a race track from OpenStreetMap.")
    parser.add_argument("--track", choices=list(TRACKS), default="zolder")
    parser.add_argument(
        "--search-radius",
        type=float,
        default=1500,
        help="Search radius around the configured track center, in metres.",
    )
    args = parser.parse_args()
    cfg = TRACKS[args.track]

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    tags = {"highway": "raceway"}

    print(f"Downloading OSM features for {cfg['name']} area...")
    gdf = ox.features_from_point(
        (cfg["lat"], cfg["lon"]),
        tags=tags,
        dist=args.search_radius,
    )

    if gdf.empty:
        raise RuntimeError("No OSM features found with highway=raceway in bounding box.")

    gdf = gdf.to_crs(WGS84)
    line = ensure_single_linestring(gdf)

    out_gdf = gpd.GeoDataFrame(
        {"name": [f"{cfg['name']} Track"]},
        geometry=[line],
        crs=WGS84,
    )

    out_gdf.to_file(cfg["track_osm_geojson"], driver="GeoJSON")
    print(f"Saved track polyline to: {cfg['track_osm_geojson']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
