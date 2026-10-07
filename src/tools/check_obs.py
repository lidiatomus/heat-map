import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from config import TRACKS


def main():
    parser = argparse.ArgumentParser(description="Plot a track and its OSM obstacles.")
    parser.add_argument("--track", choices=list(TRACKS), default="zolder")
    args = parser.parse_args()
    cfg = TRACKS[args.track]

    track = gpd.read_file(cfg["track_osm_geojson"])
    obs = gpd.read_file(cfg["obstacles_geojson"])

    fig, ax = plt.subplots(figsize=(10, 10))
    obs.plot(ax=ax, alpha=0.5)
    track.plot(ax=ax, color="red", linewidth=3)
    ax.set_title(f"{cfg['name']} obstacles")
    plt.show()


if __name__ == "__main__":
    main()
