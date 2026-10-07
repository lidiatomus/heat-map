from __future__ import annotations

import argparse

import geopandas as gpd
import pandas as pd

from config import METRIC_CRS, TRACKS as CONFIG_TRACKS
from ideal_racing_line import WeatherCache, compute_ideal_racing_line


def prepare_track(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "sector" not in df.columns:
        track_length = float(df["distance_m"].max())
        df["sector"] = pd.cut(
            df["distance_m"],
            bins=[0, track_length / 3, 2 * track_length / 3, track_length],
            labels=["Sector 1", "Sector 2", "Sector 3"],
            include_lowest=True,
        )
    return df


def load_obstacles(path):
    if path is not None and path.exists():
        return gpd.read_file(path).to_crs(METRIC_CRS)
    return gpd.GeoDataFrame(geometry=[], crs=METRIC_CRS)


def main():
    parser = argparse.ArgumentParser(description="Generate ideal solar racing line from track boundaries.")
    parser.add_argument("--track", choices=list(CONFIG_TRACKS.keys()), default="zolder")
    parser.add_argument("--time", default="now", help="Timestamp, e.g. '2026-06-03 17:30'. Default: now")
    parser.add_argument("--boundary-file", default=None, help="Optional custom boundary GeoJSON path")
    parser.add_argument("--no-weather-api", action="store_true", help="Use pvlib clear-sky only")
    parser.add_argument("--lane-change-penalty", type=float, default=0.08)
    args = parser.parse_args()

    cfg = CONFIG_TRACKS[args.track].copy()
    cfg["output_prefix"] = cfg.get("output_prefix", args.track)

    df = pd.read_csv(cfg["track_3d_csv"])
    df = prepare_track(df)

    # For Bucharest, prefer real track center from CSV, not general city center.
    if args.track == "bucharest":
        cfg["lat"] = float(df["lat"].mean())
        cfg["lon"] = float(df["lon"].mean())

    if args.time == "now":
        timestamp = pd.Timestamp.now(tz=cfg["timezone"])
    else:
        timestamp = pd.Timestamp(args.time, tz=cfg["timezone"])

    obstacles = load_obstacles(cfg.get("obstacles_geojson"))
    obstacle_sindex = obstacles.sindex if not obstacles.empty else None
    weather_cache = WeatherCache(enabled=not args.no_weather_api)

    result = compute_ideal_racing_line(
        df=df,
        cfg=cfg,
        timestamp=timestamp,
        obstacles_metric=obstacles,
        obstacle_sindex=obstacle_sindex,
        weather_cache=weather_cache,
        boundary_path=args.boundary_file,
        lane_change_penalty=args.lane_change_penalty,
        save_outputs=True,
    )

    if not result["available"]:
        print(result["reason"])
        return

    print("Ideal solar racing line generated.")
    print(f"Boundary file: {result['boundary_path']}")
    print(f"Weather source: {result['weather_source']}")
    for name, path in result["outputs"].items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
