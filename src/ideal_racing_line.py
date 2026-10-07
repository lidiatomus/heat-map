from __future__ import annotations

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pvlib.location import Location
from shapely.geometry import LineString

from config import DATA_PROCESSED, OUTPUT_PLOTS, METRIC_CRS

# ==========================================================
# Ideal Racing Line helper
# ----------------------------------------------------------
# Uses:
# - track boundaries GeoJSON: inner + outer LineString
# - same solar ideas as 04/08/live dashboard:
#   GHI, solar azimuth, solar zenith, heading, pitch/slope, shadows
# - Open-Meteo weather forecast if enabled
#
# Expected boundary file:
#   data/processed/<prefix>_track_boundaries.geojson
# Example:
#   data/processed/zolder_track_boundaries.geojson
#
# Output:
#   outputs/plots/<prefix>_ideal_racing_line_live.geojson
#   outputs/plots/<prefix>_ideal_racing_line_live.csv
#   outputs/plots/<prefix>_ideal_racing_line_live.png
# ==========================================================

SOLAR_PANEL_AREA_M2 = 4.0
PANEL_EFFICIENCY = 0.24
DIRECTION_WEIGHT = 0.1
RAY_LENGTH_M = 20
SHADOW_MIN_FACTOR = 0.65
USE_DAYLIGHT_ONLY = True
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Candidate positions between the two drawn track boundaries.
# 0.0 = first boundary, 1.0 = second boundary.
DEFAULT_LANE_ALPHAS = [0.10, 0.25, 0.50, 0.75, 0.90]


class WeatherCache:
    def __init__(self, enabled: bool = True, refresh_seconds: int = 3600):
        self.enabled = enabled
        self.refresh_seconds = refresh_seconds
        self.last_fetch_ts = 0.0
        self.df = None
        self.source = "clear-sky"

    def fetch_if_needed(self, lat: float, lon: float, timezone: str):
        if not self.enabled:
            self.source = "clear-sky"
            return None

        now = time.time()
        if self.df is not None and now - self.last_fetch_ts < self.refresh_seconds:
            return self.df

        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": timezone,
            "forecast_days": 2,
            "hourly": ",".join([
                "shortwave_radiation",
                "direct_radiation",
                "diffuse_radiation",
                "cloud_cover",
            ]),
        }

        url = OPEN_METEO_URL + "?" + urlencode(params)

        try:
            with urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            if not times:
                raise ValueError("Open-Meteo response has no hourly time values.")

            weather_df = pd.DataFrame({
                "time": pd.to_datetime(times).tz_localize(timezone),
                "shortwave_radiation": hourly.get("shortwave_radiation", [np.nan] * len(times)),
                "direct_radiation": hourly.get("direct_radiation", [np.nan] * len(times)),
                "diffuse_radiation": hourly.get("diffuse_radiation", [np.nan] * len(times)),
                "cloud_cover": hourly.get("cloud_cover", [np.nan] * len(times)),
            })

            self.df = weather_df
            self.last_fetch_ts = now
            self.source = "Open-Meteo forecast"
            return self.df

        except Exception as exc:
            print(f"[weather] Open-Meteo unavailable, using clear-sky only: {exc}")
            self.source = "clear-sky fallback"
            return None

    def weather_base_ghi(self, timestamp, clear_sky_ghi: float, lat: float, lon: float, timezone: str) -> float:
        weather_df = self.fetch_if_needed(lat, lon, timezone)
        if weather_df is None or weather_df.empty:
            return float(clear_sky_ghi)

        target = pd.Timestamp(timestamp)
        if target.tzinfo is None:
            target = target.tz_localize(timezone)
        else:
            target = target.tz_convert(timezone)

        ts_seconds = target.timestamp()
        weather_seconds = weather_df["time"].map(lambda x: x.timestamp()).to_numpy()

        sw = pd.to_numeric(weather_df["shortwave_radiation"], errors="coerce").to_numpy(dtype=float)
        cloud = pd.to_numeric(weather_df["cloud_cover"], errors="coerce").to_numpy(dtype=float)

        if len(weather_seconds) >= 2 and np.isfinite(sw).any():
            valid = np.isfinite(sw)
            weather_ghi = np.interp(ts_seconds, weather_seconds[valid], sw[valid])
            if np.isfinite(weather_ghi):
                return max(float(weather_ghi), 0.0)

        if len(weather_seconds) >= 2 and np.isfinite(cloud).any():
            valid = np.isfinite(cloud)
            cloud_value = np.interp(ts_seconds, weather_seconds[valid], cloud[valid])
            cloud_factor = 1.0 - 0.75 * np.clip(cloud_value, 0, 100) / 100.0
            cloud_factor = np.clip(cloud_factor, 0.15, 1.0)
            return float(clear_sky_ghi) * float(cloud_factor)

        return float(clear_sky_ghi)


def default_boundary_path(cfg) -> object:
    prefix = cfg.get("output_prefix", cfg.get("name", "track")).lower()
    return DATA_PROCESSED / f"{prefix}_track_boundaries.geojson"


def _reverse_linestring(line: LineString) -> LineString:
    return LineString(list(line.coords)[::-1])


def load_boundaries(cfg, boundary_path=None):
    path = boundary_path or default_boundary_path(cfg)
    if not os.path.exists(path):
        return None, None, path

    gdf = gpd.read_file(path).to_crs(METRIC_CRS)
    lines = []
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            lines.append(geom)
        elif geom.geom_type == "MultiLineString":
            lines.extend(list(geom.geoms))

    if len(lines) < 2:
        raise ValueError(f"Boundary file must contain at least 2 LineStrings: {path}")

    # Use the two longest lines, because sometimes GeoJSON contains extra small fragments.
    lines = sorted(lines, key=lambda x: x.length, reverse=True)[:2]
    line_a, line_b = lines[0], lines[1]

    # Make both boundaries go in the same direction.
    a0 = line_a.interpolate(0)
    b0 = line_b.interpolate(0)
    b1 = line_b.interpolate(line_b.length)
    if a0.distance(b1) < a0.distance(b0):
        line_b = _reverse_linestring(line_b)

    return line_a, line_b, path


def prepare_centerline_df(df: pd.DataFrame) -> pd.DataFrame:
    required = ["point_id", "lon", "lat", "distance_m", "heading_deg", "slope_deg"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"{col} missing. Run 02_sample_evaluation.py first.")

    out = df.copy().reset_index(drop=True)
    if "sector" not in out.columns:
        track_length = float(out["distance_m"].max())
        out["sector"] = pd.cut(
            out["distance_m"],
            bins=[0, track_length / 3, 2 * track_length / 3, track_length],
            labels=["Sector 1", "Sector 2", "Sector 3"],
            include_lowest=True,
        )
    return out


def build_candidate_points(df: pd.DataFrame, line_a: LineString, line_b: LineString, lane_alphas=None) -> gpd.GeoDataFrame:
    lane_alphas = lane_alphas or DEFAULT_LANE_ALPHAS
    df = prepare_centerline_df(df)
    track_length = max(float(df["distance_m"].max()), 1.0)

    rows = []
    for i, row in df.iterrows():
        frac = float(row["distance_m"]) / track_length
        frac = float(np.clip(frac, 0.0, 1.0))

        pa = line_a.interpolate(frac * line_a.length)
        pb = line_b.interpolate(frac * line_b.length)

        for lane_index, alpha in enumerate(lane_alphas):
            x = pa.x + alpha * (pb.x - pa.x)
            y = pa.y + alpha * (pb.y - pa.y)
            rows.append({
                "center_index": i,
                "point_id": int(row["point_id"]),
                "distance_m": float(row["distance_m"]),
                "distance_km": float(row.get("distance_km", row["distance_m"] / 1000.0)),
                "sector": row["sector"],
                "heading_deg": float(row["heading_deg"]),
                "slope_deg": float(row["slope_deg"]),
                "lane_index": lane_index,
                "lane_alpha": float(alpha),
                "x_m": float(x),
                "y_m": float(y),
            })

    gdf = gpd.GeoDataFrame(
        rows,
        geometry=gpd.points_from_xy([r["x_m"] for r in rows], [r["y_m"] for r in rows]),
        crs=METRIC_CRS,
    )
    wgs = gdf.to_crs("EPSG:4326")
    gdf["lon"] = wgs.geometry.x
    gdf["lat"] = wgs.geometry.y
    return gdf


def compute_shadow_factor_for_candidates(candidates: gpd.GeoDataFrame, obstacles_metric, obstacle_sindex, solar_azimuth_value: float):
    shadow_factor = np.ones(len(candidates))
    if obstacles_metric is None or obstacles_metric.empty or obstacle_sindex is None:
        return shadow_factor

    az = float(solar_azimuth_value)
    dx = -RAY_LENGTH_M * np.sin(np.radians(az))
    dy = -RAY_LENGTH_M * np.cos(np.radians(az))

    for pos, (_, row) in enumerate(candidates.iterrows()):
        x = float(row["x_m"])
        y = float(row["y_m"])
        ray = LineString([(x, y), (x + dx, y + dy)])
        possible_idx = list(obstacle_sindex.intersection(ray.bounds))
        if not possible_idx:
            continue

        hits = obstacles_metric.iloc[possible_idx]
        hits = hits[hits.intersects(ray)]
        if len(hits) > 0:
            distances = hits.distance(ray.centroid)
            min_distance = float(distances.min())
            shadow_factor[pos] = np.interp(min_distance, [0, RAY_LENGTH_M], [SHADOW_MIN_FACTOR, 1.0])
            shadow_factor[pos] = np.clip(shadow_factor[pos], SHADOW_MIN_FACTOR, 1.0)

    return shadow_factor


def compute_candidate_solar_scores(candidates: gpd.GeoDataFrame, cfg, timestamp, obstacles_metric, obstacle_sindex, weather_cache=None):
    timezone = cfg["timezone"]
    location = Location(float(cfg["lat"]), float(cfg["lon"]), tz=timezone)
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(timezone)
    else:
        timestamp = timestamp.tz_convert(timezone)

    point_times = pd.DatetimeIndex([timestamp])
    clear_sky = location.get_clearsky(point_times)
    solar_position = location.get_solarposition(point_times)

    clear_sky_ghi = float(clear_sky["ghi"].iloc[0])
    weather_cache = weather_cache or WeatherCache(enabled=True)
    base_ghi = weather_cache.weather_base_ghi(
        timestamp,
        clear_sky_ghi,
        float(cfg["lat"]),
        float(cfg["lon"]),
        timezone,
    )

    solar_azimuth = float(solar_position["azimuth"].iloc[0])
    solar_zenith = float(solar_position["zenith"].iloc[0])

    if USE_DAYLIGHT_ONLY and solar_zenith >= 90:
        candidates = candidates.copy()
        candidates["candidate_irradiance_wm2"] = 0.0
        candidates["candidate_power_w"] = 0.0
        candidates["candidate_shadow_factor"] = 1.0
        candidates["solar_score"] = 0.0
        return candidates, weather_cache.source

    angle_diff = np.abs(solar_azimuth - candidates["heading_deg"].to_numpy(dtype=float))
    angle_diff = np.minimum(angle_diff, 360 - angle_diff)

    orientation_factor = np.maximum(np.cos(np.radians(angle_diff)), 0)
    zenith_factor = max(np.cos(np.radians(solar_zenith)), 0)
    pitch_factor = np.maximum(np.cos(np.radians(candidates["slope_deg"].to_numpy(dtype=float))), 0.85)
    shadow_factor = compute_shadow_factor_for_candidates(
        candidates,
        obstacles_metric,
        obstacle_sindex,
        solar_azimuth,
    )

    irradiance = base_ghi * (0.9 + DIRECTION_WEIGHT * orientation_factor)
    irradiance *= zenith_factor
    irradiance *= pitch_factor
    irradiance *= shadow_factor

    candidates = candidates.copy()
    candidates["candidate_irradiance_wm2"] = irradiance
    candidates["candidate_power_w"] = irradiance * SOLAR_PANEL_AREA_M2 * PANEL_EFFICIENCY
    candidates["candidate_shadow_factor"] = shadow_factor

    max_irr = max(float(np.max(irradiance)), 1.0)
    candidates["solar_score"] = (
        0.7 * (candidates["candidate_irradiance_wm2"] / max_irr)
        + 0.3 * candidates["candidate_shadow_factor"]
    )

    return candidates, weather_cache.source


def choose_smooth_ideal_line(candidates: gpd.GeoDataFrame, lane_change_penalty: float = 0.08) -> pd.DataFrame:
    """Dynamic programming: choose best lane per point while avoiding left-right jumps."""
    cand = candidates.copy().sort_values(["center_index", "lane_index"]).reset_index(drop=True)
    indices = sorted(cand["center_index"].unique())
    lane_values = sorted(cand["lane_index"].unique())
    n_lanes = len(lane_values)

    dp = np.full((len(indices), n_lanes), np.inf)
    prev = np.full((len(indices), n_lanes), -1, dtype=int)

    by_index = {
        idx: cand[cand["center_index"] == idx].sort_values("lane_index").reset_index(drop=True)
        for idx in indices
    }

    first = by_index[indices[0]]
    dp[0, :] = -first["solar_score"].to_numpy(dtype=float)

    for t in range(1, len(indices)):
        rows = by_index[indices[t]]
        scores = rows["solar_score"].to_numpy(dtype=float)
        for j in range(n_lanes):
            costs = dp[t - 1, :] + lane_change_penalty * np.abs(np.arange(n_lanes) - j)
            best_prev = int(np.argmin(costs))
            dp[t, j] = costs[best_prev] - scores[j]
            prev[t, j] = best_prev

    lane_path = np.zeros(len(indices), dtype=int)
    lane_path[-1] = int(np.argmin(dp[-1, :]))
    for t in range(len(indices) - 1, 0, -1):
        lane_path[t - 1] = prev[t, lane_path[t]]

    selected_rows = []
    for t, idx in enumerate(indices):
        rows = by_index[idx]
        selected_rows.append(rows.iloc[lane_path[t]])

    ideal = pd.DataFrame(selected_rows).reset_index(drop=True)
    return ideal


def save_ideal_line_outputs(ideal_df: pd.DataFrame, candidates: gpd.GeoDataFrame, cfg, timestamp, boundary_path=None):
    OUTPUT_PLOTS.mkdir(parents=True, exist_ok=True)
    prefix = cfg["output_prefix"]

    csv_path = OUTPUT_PLOTS / f"{prefix}_ideal_racing_line_live.csv"
    geojson_path = OUTPUT_PLOTS / f"{prefix}_ideal_racing_line_live.geojson"
    png_path = OUTPUT_PLOTS / f"{prefix}_ideal_racing_line_live.png"
    candidates_path = OUTPUT_PLOTS / f"{prefix}_ideal_racing_line_candidates_live.csv"

    ideal_df.to_csv(csv_path, index=False)
    candidates.drop(columns="geometry").to_csv(candidates_path, index=False)

    line = LineString(list(zip(ideal_df["lon"], ideal_df["lat"])))
    ideal_gdf = gpd.GeoDataFrame(
        [{
            "track": cfg["name"],
            "updated_at": str(timestamp),
            "boundary_file": str(boundary_path) if boundary_path is not None else "",
        }],
        geometry=[line],
        crs="EPSG:4326",
    )
    ideal_gdf.to_file(geojson_path, driver="GeoJSON")

    fig, ax = plt.subplots(figsize=(10, 10))
    sc = ax.scatter(
        candidates["lon"],
        candidates["lat"],
        c=candidates["candidate_irradiance_wm2"],
        s=8,
    )
    fig.colorbar(sc, ax=ax, label="Candidate irradiance (W/m²)")
    ax.plot(ideal_df["lon"], ideal_df["lat"], linewidth=3, label="Ideal racing line")
    ax.scatter(ideal_df["lon"].iloc[0], ideal_df["lat"].iloc[0], s=40, marker="o", label="Start")
    ax.set_title(f"{cfg['name']} ideal solar racing line | {pd.Timestamp(timestamp).strftime('%Y-%m-%d %H:%M')}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    ax.legend()
    fig.tight_layout()
    fig.savefig(png_path, dpi=200)
    plt.close(fig)

    return {
        "csv_path": csv_path,
        "geojson_path": geojson_path,
        "png_path": png_path,
        "candidates_path": candidates_path,
    }


def compute_ideal_racing_line(
    df: pd.DataFrame,
    cfg,
    timestamp,
    obstacles_metric=None,
    obstacle_sindex=None,
    weather_cache=None,
    boundary_path=None,
    lane_alphas=None,
    lane_change_penalty: float = 0.08,
    save_outputs: bool = True,
):
    line_a, line_b, used_boundary_path = load_boundaries(cfg, boundary_path)
    if line_a is None or line_b is None:
        return {
            "available": False,
            "reason": f"Boundary file not found: {used_boundary_path}",
            "boundary_path": used_boundary_path,
        }

    candidates = build_candidate_points(df, line_a, line_b, lane_alphas=lane_alphas)
    candidates, weather_source = compute_candidate_solar_scores(
        candidates,
        cfg,
        timestamp,
        obstacles_metric,
        obstacle_sindex,
        weather_cache=weather_cache,
    )
    ideal_df = choose_smooth_ideal_line(candidates, lane_change_penalty=lane_change_penalty)

    outputs = {}
    if save_outputs:
        outputs = save_ideal_line_outputs(ideal_df, candidates, cfg, timestamp, boundary_path=used_boundary_path)

    return {
        "available": True,
        "boundary_path": used_boundary_path,
        "weather_source": weather_source,
        "ideal_df": ideal_df,
        "candidates": candidates,
        "outputs": outputs,
    }
