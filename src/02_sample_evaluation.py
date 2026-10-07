from __future__ import annotations

import argparse
import sys
from typing import List

import geopandas as gpd
import pandas as pd
import rasterio
from rasterio.sample import sample_gen
from shapely.geometry import Point
import numpy as np

from config import (
    DATA_PROCESSED,
    DENSIFY_STEP_METERS,
    METRIC_CRS,
    TRACKS,
    WGS84,
)
from utils_geo import cumulative_distance, densify_linestring, points_to_dataframe

def main() -> None:
    parser = argparse.ArgumentParser(description="Sample track elevation from its DEM.")
    parser.add_argument("--track", choices=list(TRACKS), default="zolder")
    args = parser.parse_args()
    track_cfg = TRACKS[args.track]
    dem_path = track_cfg["dem_path"]
    track_osm_geojson = track_cfg["track_osm_geojson"]
    track_3d_csv = track_cfg["track_3d_csv"]
    track_3d_geojson = track_cfg["track_3d_geojson"]

    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    if not track_osm_geojson.exists():
        raise FileNotFoundError(
            f"Track file not found: {track_osm_geojson}. "
            f"Run 01_fetch_track.py --track {args.track} first."
        )

    if not dem_path.exists():
        raise FileNotFoundError(
            f"DEM file not found: {dem_path}. Place your DEM there first."
        )

    print("Reading track geometry...")
    track_gdf = gpd.read_file(track_osm_geojson)
    track_metric = track_gdf.to_crs(METRIC_CRS)
    line_metric = track_metric.geometry.iloc[0]

    print("Densifying track...")
    points_metric = densify_linestring(line_metric, DENSIFY_STEP_METERS)
    points_metric_gdf = gpd.GeoDataFrame(geometry=points_metric, crs=METRIC_CRS)

    print("Projecting points back to WGS84...")
    points_wgs84_gdf = points_metric_gdf.to_crs(WGS84)
    points_wgs84: List[Point] = list(points_wgs84_gdf.geometry)

    with rasterio.open(dem_path) as src:
        print("Reprojecting points to DEM CRS...")
        points_dem_crs = points_wgs84_gdf.to_crs(src.crs)

        coords = [(pt.x, pt.y) for pt in points_dem_crs.geometry]
        print("Sampling elevation from DEM...")
        samples = list(sample_gen(src, coords))
        elevations = [float(s[0]) if s is not None else float("nan") for s in samples]

    distances_m = cumulative_distance(points_metric)
    df = points_to_dataframe(points_wgs84, elevations, distances_m)

    headings = []

    for i in range(len(df)-1):

        lon1 = np.radians(df.loc[i,"lon"])
        lat1 = np.radians(df.loc[i,"lat"])

        lon2 = np.radians(df.loc[i+1,"lon"])
        lat2 = np.radians(df.loc[i+1,"lat"])

        dlon = lon2-lon1

        x = np.sin(dlon)*np.cos(lat2)

        y = (
            np.cos(lat1)*np.sin(lat2)
            - np.sin(lat1)*np.cos(lat2)*np.cos(dlon)
        )

        heading = np.degrees(np.arctan2(x,y))

        heading = (heading+360)%360

        headings.append(heading)

    headings.append(headings[-1])

    df["heading_deg"] = headings

    elevation_diff = df["elevation_m"].diff()
    distance_diff = df["distance_m"].diff()

    df["slope"] = elevation_diff / distance_diff
    df["slope"] = df["slope"].replace([np.inf, -np.inf], 0)
    df["slope"] = df["slope"].fillna(0)

    # Panta in grade, folosita ca aproximare pentru pitch-ul masinii
    df["slope_deg"] = np.degrees(np.arctan(df["slope"]))

    print(f"Saving CSV: {track_3d_csv}")
    df.to_csv(track_3d_csv, index=False)

    print(f"Saving GeoJSON: {track_3d_geojson}")
    points_out = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs=WGS84,
    )
    points_out.to_file(track_3d_geojson, driver="GeoJSON")

    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
