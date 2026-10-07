import argparse
import osmnx as ox

from config import DATA_PROCESSED, TRACKS


def main():
    parser = argparse.ArgumentParser(description="Download track obstacles from OpenStreetMap.")
    parser.add_argument("--track", choices=list(TRACKS), default="zolder")
    parser.add_argument(
        "--search-radius",
        type=float,
        default=1500,
        help="Search radius around the configured track center, in metres.",
    )
    args = parser.parse_args()
    cfg = TRACKS[args.track]

    tags = {
        "building": True,
        "landuse": ["forest"],
        "natural": ["tree", "wood"]
    }

    print(f"Downloading obstacles for {cfg['name']} from OSM...")

    gdf = ox.features_from_point(
        (cfg["lat"], cfg["lon"]),
        tags=tags,
        dist=args.search_radius,
    )

    gdf = gdf[
        gdf.geometry.notnull()
    ]

    print(f"Found {len(gdf)} obstacles")

    gdf.to_file(
        cfg["obstacles_geojson"],
        driver="GeoJSON"
    )

    print("Saved:")
    print(cfg["obstacles_geojson"])


if __name__=="__main__":
    main()
