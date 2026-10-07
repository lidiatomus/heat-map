from __future__ import annotations

import argparse
import sys

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd

from config import OUTPUT_PLOTS, TRACKS, WGS84


def plot_track_map(gdf: gpd.GeoDataFrame, name: str, output_prefix: str) -> None:
    fig, ax = plt.subplots(figsize=(10, 10))
    gdf.plot(
        ax=ax,
        column="elevation_m",
        legend=True,
        markersize=20,
    )
    ax.set_title(f"{name} - sampled track points colored by elevation")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")

    out_path = OUTPUT_PLOTS / f"{output_prefix}_track_altitude_map.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)

    print(f"Saved map plot to: {out_path}")


def plot_elevation_profile(df: pd.DataFrame, name: str, output_prefix: str) -> None:
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df["distance_km"], df["elevation_m"])
    ax.set_title(f"{name} - elevation profile")
    ax.set_xlabel("Distance along track [km]")
    ax.set_ylabel("Elevation [m]")

    out_path = OUTPUT_PLOTS / f"{output_prefix}_track_elevation_profile.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)

    print(f"Saved elevation profile to: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot a processed race track.")
    parser.add_argument("--track", choices=list(TRACKS), default="zolder")
    args = parser.parse_args()
    cfg = TRACKS[args.track]

    OUTPUT_PLOTS.mkdir(parents=True, exist_ok=True)

    if not cfg["track_3d_csv"].exists() or not cfg["track_3d_geojson"].exists():
        raise FileNotFoundError(
            f"Missing processed track files. Run "
            f"02_sample_evaluation.py --track {args.track} first."
        )

    df = pd.read_csv(cfg["track_3d_csv"])
    gdf = gpd.read_file(cfg["track_3d_geojson"]).to_crs(WGS84)

    plot_track_map(gdf, cfg["name"], cfg["output_prefix"])
    plot_elevation_profile(df, cfg["name"], cfg["output_prefix"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
