from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point, MultiLineString
from shapely.ops import linemerge, unary_union
import geopandas as gpd


def _reverse_coords_if_needed(coords, ref_point):
    start_dist = Point(coords[0]).distance(ref_point)
    end_dist = Point(coords[-1]).distance(ref_point)
    if end_dist < start_dist:
        return list(coords)[::-1]
    return list(coords)


def stitch_multilinestring(multiline: MultiLineString) -> LineString:
    """
    Stitch multiple line segments into a single LineString by greedily
    connecting the nearest segment endpoints.
    """
    segments = [LineString(geom.coords) for geom in multiline.geoms if not geom.is_empty]
    if not segments:
        raise ValueError("No segments to stitch.")

    # start from the longest segment
    segments.sort(key=lambda g: g.length, reverse=True)
    current = segments.pop(0)
    stitched_coords = list(current.coords)

    while segments:
        current_end = Point(stitched_coords[-1])

        best_idx = None
        best_coords = None
        best_dist = float("inf")

        for i, seg in enumerate(segments):
            coords = list(seg.coords)
            start_pt = Point(coords[0])
            end_pt = Point(coords[-1])

            d_start = current_end.distance(start_pt)
            d_end = current_end.distance(end_pt)

            if d_start < best_dist:
                best_dist = d_start
                best_idx = i
                best_coords = coords

            if d_end < best_dist:
                best_dist = d_end
                best_idx = i
                best_coords = coords[::-1]

        chosen = best_coords

        # avoid duplicating the first point if it is basically the same
        if Point(stitched_coords[-1]).distance(Point(chosen[0])) < 1e-6:
            stitched_coords.extend(chosen[1:])
        else:
            stitched_coords.extend(chosen)

        segments.pop(best_idx)

    return LineString(stitched_coords)


def ensure_single_linestring(gdf: gpd.GeoDataFrame) -> LineString:
    """
    Merge multiple line segments into a single LineString where possible.
    If the result is still fragmented, stitch segments together heuristically.
    """
    if gdf.empty:
        raise ValueError("GeoDataFrame is empty.")

    geometries = [geom for geom in gdf.geometry if geom is not None and not geom.is_empty]
    if not geometries:
        raise ValueError("No valid geometries found.")

    merged = linemerge(unary_union(geometries))

    if merged.geom_type == "LineString":
        return merged

    if merged.geom_type == "MultiLineString":
        return stitch_multilinestring(merged)

    raise ValueError(f"Unexpected merged geometry type: {merged.geom_type}")


def densify_linestring(line: LineString, step_meters: float) -> List[Point]:
    """
    Densify a metric LineString by sampling points every `step_meters`.
    """
    if step_meters <= 0:
        raise ValueError("step_meters must be > 0")

    length = line.length
    distances = np.arange(0, length + step_meters, step_meters)
    points = [line.interpolate(float(d)) for d in distances]

    if points[-1].distance(Point(line.coords[-1])) > 1e-6:
        points.append(Point(line.coords[-1]))

    return points


def cumulative_distance(points: List[Point]) -> np.ndarray:
    """
    Compute cumulative distance along a list of metric points.
    """
    if len(points) < 2:
        return np.array([0.0])

    distances = [0.0]
    total = 0.0
    for i in range(1, len(points)):
        total += points[i - 1].distance(points[i])
        distances.append(total)

    return np.array(distances)


def points_to_dataframe(
    points_wgs84: List[Point],
    elevations: List[float],
    distances_m: np.ndarray,
) -> pd.DataFrame:
    """
    Build a DataFrame from sampled points.
    """
    rows = []
    for i, (pt, elev, dist_m) in enumerate(zip(points_wgs84, elevations, distances_m)):
        rows.append(
            {
                "point_id": i,
                "lon": pt.x,
                "lat": pt.y,
                "elevation_m": elev,
                "distance_m": float(dist_m),
                "distance_km": float(dist_m) / 1000.0,
            }
        )

    return pd.DataFrame(rows)