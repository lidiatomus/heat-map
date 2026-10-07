import argparse
import json
import os
import time
from pathlib import Path
from collections import deque
from datetime import timedelta
from urllib.parse import urlencode
from urllib.request import urlopen

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.geometry import LineString
from pvlib.location import Location

from config import OUTPUT_PLOTS, DATA_PROCESSED, METRIC_CRS, TRACKS as CONFIG_TRACKS
from ideal_racing_line import compute_ideal_racing_line
from influx_gps import InfluxGPSClient



DEFAULT_WINDOW_MINUTES = 15
DEFAULT_UPDATE_SECONDS = 60
DEFAULT_TOP_SEGMENTS = 15

SOLAR_PANEL_AREA_M2 = 4.0
PANEL_EFFICIENCY = 0.24
DIRECTION_WEIGHT = 0.1
RAY_LENGTH_M = 20
SHADOW_MIN_FACTOR = 0.65
USE_DAYLIGHT_ONLY = True

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


class WeatherCache:
    """
    Cache for Open-Meteo values.

    For current timestamps it uses the Forecast API.
    For historical timestamps it uses the Historical Weather API.
    """

    def __init__(self, enabled=True, refresh_seconds=3600):
        self.enabled = enabled
        self.refresh_seconds = refresh_seconds
        self.last_fetch_ts = 0
        self.df = None
        self.source = "clear-sky"
        self.cache_key = None

    def fetch_if_needed(self, lat, lon, timezone, timestamp):
        if not self.enabled:
            self.source = "clear-sky"
            return None

        target = pd.Timestamp(timestamp)
        if target.tzinfo is None:
            target = target.tz_localize(timezone)
        else:
            target = target.tz_convert(timezone)

        now_local = pd.Timestamp.now(tz=timezone)
        is_historical = target.date() < (now_local - pd.Timedelta(days=5)).date()

        if is_historical:
            start_date = (target - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            end_date = (target + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            cache_key = ("historical", start_date, end_date, round(lat, 5), round(lon, 5))
            endpoint = OPEN_METEO_ARCHIVE_URL
            params = {
                "latitude": lat,
                "longitude": lon,
                "timezone": timezone,
                "start_date": start_date,
                "end_date": end_date,
                "hourly": ",".join([
                    "shortwave_radiation",
                    "direct_radiation",
                    "diffuse_radiation",
                    "cloud_cover",
                ]),
            }
            source_name = "Open-Meteo historical"
        else:
            cache_key = ("forecast", target.strftime("%Y-%m-%d"), round(lat, 5), round(lon, 5))
            endpoint = OPEN_METEO_URL
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
            source_name = "Open-Meteo forecast"

        now_epoch = time.time()
        if (
            self.df is not None
            and self.cache_key == cache_key
            and now_epoch - self.last_fetch_ts < self.refresh_seconds
        ):
            return self.df

        url = endpoint + "?" + urlencode(params)

        try:
            with urlopen(url, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            if not times:
                raise ValueError("Open-Meteo response has no hourly time values.")

            weather_df = pd.DataFrame({
                "time": pd.to_datetime(times).tz_localize(timezone),
                "shortwave_radiation": hourly.get(
                    "shortwave_radiation", [np.nan] * len(times)
                ),
                "direct_radiation": hourly.get(
                    "direct_radiation", [np.nan] * len(times)
                ),
                "diffuse_radiation": hourly.get(
                    "diffuse_radiation", [np.nan] * len(times)
                ),
                "cloud_cover": hourly.get(
                    "cloud_cover", [np.nan] * len(times)
                ),
            })

            self.df = weather_df
            self.cache_key = cache_key
            self.last_fetch_ts = now_epoch
            self.source = source_name
            return self.df

        except Exception as exc:
            print(f"[weather] Open-Meteo unavailable, using clear-sky only: {exc}")
            self.source = "clear-sky fallback"
            return None

    def weather_base_ghi(self, timestamp, clear_sky_ghi, lat, lon, timezone):
        weather_df = self.fetch_if_needed(
            lat=lat,
            lon=lon,
            timezone=timezone,
            timestamp=timestamp,
        )

        if weather_df is None or weather_df.empty:
            return clear_sky_ghi

        target = pd.Timestamp(timestamp)
        if target.tzinfo is None:
            target = target.tz_localize(timezone)
        else:
            target = target.tz_convert(timezone)

        ts_seconds = target.timestamp()
        weather_seconds = (
            weather_df["time"]
            .map(lambda value: value.timestamp())
            .to_numpy()
        )

        # Never extrapolate a forecast/historical response outside its interval.
        if (
            len(weather_seconds) == 0
            or ts_seconds < weather_seconds.min()
            or ts_seconds > weather_seconds.max()
        ):
            self.source = "clear-sky time-range fallback"
            return float(max(clear_sky_ghi, 0.0))

        shortwave = pd.to_numeric(
            weather_df["shortwave_radiation"],
            errors="coerce",
        ).to_numpy(dtype=float)

        cloud = pd.to_numeric(
            weather_df["cloud_cover"],
            errors="coerce",
        ).to_numpy(dtype=float)

        if len(weather_seconds) >= 2 and np.isfinite(shortwave).any():
            valid = np.isfinite(shortwave)
            weather_ghi = np.interp(
                ts_seconds,
                weather_seconds[valid],
                shortwave[valid],
            )
            if np.isfinite(weather_ghi):
                return float(max(weather_ghi, 0.0))

        if len(weather_seconds) >= 2 and np.isfinite(cloud).any():
            valid = np.isfinite(cloud)
            cloud_value = np.interp(
                ts_seconds,
                weather_seconds[valid],
                cloud[valid],
            )
            cloud_factor = 1.0 - 0.75 * np.clip(
                cloud_value, 0, 100
            ) / 100.0
            cloud_factor = np.clip(cloud_factor, 0.15, 1.0)
            return float(clear_sky_ghi * cloud_factor)

        return float(clear_sky_ghi)


def build_track_catalog():
    catalog = {}
    for key, cfg in CONFIG_TRACKS.items():
        catalog[key] = {
            "name": cfg.get("name", key),
            "lat": cfg.get("lat"),
            "lon": cfg.get("lon"),
            "timezone": cfg.get("timezone"),
            "track_3d_csv": cfg.get("track_3d_csv"),
            "obstacles_geojson": cfg.get("obstacles_geojson"),
            "output_prefix": cfg.get("output_prefix", key),
        }

    # Bucuresti din config are coordonate generale de oras, dar circuitul folosit de tine
    # era in zona Academia Titi Aur. Daca exista track-ul respectiv, folosim centrul real din CSV.
    return catalog


def choose_track(catalog, selected_track=None):
    if selected_track:
        if selected_track not in catalog:
            raise ValueError(f"Unknown track '{selected_track}'. Available: {list(catalog.keys())}")
        return selected_track, catalog[selected_track]

    print("Available tracks:")
    keys = list(catalog.keys())
    for i, key in enumerate(keys, start=1):
        print(f"  {i}. {key} - {catalog[key]['name']}")

    raw = input("Choose track number or name: ").strip().lower()
    if raw.isdigit():
        idx = int(raw) - 1
        if idx < 0 or idx >= len(keys):
            raise ValueError("Invalid track number.")
        key = keys[idx]
        return key, catalog[key]

    if raw not in catalog:
        raise ValueError(f"Invalid track name. Available: {keys}")

    return raw, catalog[raw]


def prepare_track(df):
    required = ["lon", "lat", "distance_m", "heading_deg", "slope_deg"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"{col} missing. Run 02_sample_evaluation.py first for this track.")

    df = df.copy()
    df["segment_length_m"] = df["distance_m"].diff().bfill().fillna(0)
    df["segment_center_lon"] = ((df["lon"] + df["lon"].shift(-1)) / 2).fillna(df["lon"])
    df["segment_center_lat"] = ((df["lat"] + df["lat"].shift(-1)) / 2).fillna(df["lat"])

    track_length = df["distance_m"].max()
    df["sector"] = pd.cut(
        df["distance_m"],
        bins=[0, track_length / 3, 2 * track_length / 3, track_length],
        labels=["Sector 1", "Sector 2", "Sector 3"],
        include_lowest=True,
    )

    return df


def load_track(cfg):
    path = cfg["track_3d_csv"]
    if not path.exists():
        raise FileNotFoundError(f"Missing track file: {path}. Run 02_sample_evaluation.py first.")

    df = pd.read_csv(path)
    df = prepare_track(df)

    # Use actual track center for weather coordinates, not only config coordinates.
    cfg = cfg.copy()
    cfg["lat"] = float(df["lat"].mean()) if cfg.get("lat") is None else float(cfg["lat"])
    cfg["lon"] = float(df["lon"].mean()) if cfg.get("lon") is None else float(cfg["lon"])

    if "bucharest" in cfg.get("output_prefix", ""):
        # Pentru demo-ul Bucuresti, track-ul tau e mai relevant decat centrul orasului.
        cfg["lat"] = float(df["lat"].mean())
        cfg["lon"] = float(df["lon"].mean())

    track_points = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs="EPSG:4326",
    ).to_crs(METRIC_CRS)

    df["x_m"] = track_points.geometry.x
    df["y_m"] = track_points.geometry.y

    return df, cfg


def load_obstacles(path):
    if path is not None and os.path.exists(path):
        obstacles = gpd.read_file(path)
        return obstacles.to_crs(METRIC_CRS)

    print("[obstacles] No obstacles file found. Running without shadows.")
    return gpd.GeoDataFrame(geometry=[], crs=METRIC_CRS)


def gps_is_valid_for_track(latitude, longitude, df, max_distance_km=10.0):
    """Reject null-island, malformed, or off-track GPS samples."""
    if latitude is None or longitude is None:
        return False

    latitude = float(latitude)
    longitude = float(longitude)
    if (
        not np.isfinite(latitude)
        or not np.isfinite(longitude)
        or abs(latitude) > 90
        or abs(longitude) > 180
        or (abs(latitude) < 1e-9 and abs(longitude) < 1e-9)
    ):
        return False

    track_lat = df["lat"].to_numpy(dtype=float)
    track_lon = df["lon"].to_numpy(dtype=float)
    lat1 = np.radians(latitude)
    lat2 = np.radians(track_lat)
    dlat = lat2 - lat1
    dlon = np.radians(track_lon - longitude)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    )
    distances_km = 6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return bool(np.min(distances_km) <= max_distance_km)


def compute_shadow_factor(df, obstacles_metric, obstacle_sindex, solar_azimuth):
    shadow_factor = np.ones(len(df))
    if obstacles_metric.empty or obstacle_sindex is None:
        return shadow_factor

    for i in range(len(df)):
        x = df.loc[i, "x_m"]
        y = df.loc[i, "y_m"]
        az = solar_azimuth[i]

        dx = -RAY_LENGTH_M * np.sin(np.radians(az))
        dy = -RAY_LENGTH_M * np.cos(np.radians(az))

        ray = LineString([(x, y), (x + dx, y + dy)])
        possible_idx = list(obstacle_sindex.intersection(ray.bounds))
        if not possible_idx:
            continue

        candidates = obstacles_metric.iloc[possible_idx]
        hits = candidates[candidates.intersects(ray)]

        if len(hits) > 0:
            distances = hits.distance(ray.centroid)
            min_distance = distances.min()
            shadow_factor[i] = np.interp(
                min_distance,
                [0, RAY_LENGTH_M],
                [SHADOW_MIN_FACTOR, 1.0],
            )
            shadow_factor[i] = np.clip(shadow_factor[i], SHADOW_MIN_FACTOR, 1.0)

    return shadow_factor


def compute_effective_irradiance(df, location, timestamp, obstacles_metric, obstacle_sindex, weather_cache, cfg):
    point_times = pd.DatetimeIndex([timestamp] * len(df))

    clear_sky = location.get_clearsky(point_times)
    solar_position = location.get_solarposition(point_times)

    clear_sky_ghi = float(clear_sky["ghi"].iloc[0])
    base_ghi = weather_cache.weather_base_ghi(
        timestamp,
        clear_sky_ghi,
        cfg["lat"],
        cfg["lon"],
        cfg["timezone"],
    )

    ghi = np.full(len(df), base_ghi)
    solar_azimuth = solar_position["azimuth"].to_numpy()
    solar_zenith = solar_position["zenith"].to_numpy()

    if USE_DAYLIGHT_ONLY:
        daylight_mask = solar_zenith < 90
    else:
        daylight_mask = np.ones(len(df), dtype=bool)

    smoothed_heading = (
        df["heading_deg"].rolling(window=15, center=True).mean().bfill().ffill().to_numpy()
    )

    smoothed_pitch = (
        df["slope_deg"].rolling(window=15, center=True).mean().bfill().ffill().to_numpy()
    )

    angle_diff = np.abs(solar_azimuth - smoothed_heading)
    angle_diff = np.minimum(angle_diff, 360 - angle_diff)

    orientation_factor = np.cos(np.radians(angle_diff))
    orientation_factor = np.maximum(orientation_factor, 0)

    zenith_factor = np.cos(np.radians(solar_zenith))
    zenith_factor = np.maximum(zenith_factor, 0)

    pitch_factor = np.cos(np.radians(smoothed_pitch))
    pitch_factor = np.maximum(pitch_factor, 0.85)

    shadow_factor = compute_shadow_factor(
        df,
        obstacles_metric,
        obstacle_sindex,
        solar_azimuth,
    )

    effective_irradiance = ghi * (0.9 + DIRECTION_WEIGHT * orientation_factor)
    effective_irradiance *= zenith_factor
    effective_irradiance *= pitch_factor
    effective_irradiance *= shadow_factor
    effective_irradiance[~daylight_mask] = 0

    return effective_irradiance, shadow_factor, base_ghi, clear_sky_ghi


def compute_rolling_window(df, location, now, window_minutes, obstacles_metric, obstacle_sindex, weather_cache, cfg):
    # Fereastra mobila: ultimele N minute, cu pas de 1 minut.
    # Exemplu la 14:37 -> 14:23, 14:24, ..., 14:37.
    now = pd.Timestamp(now).floor("min")
    if now.tzinfo is None:
        now = now.tz_localize(cfg["timezone"])
    else:
        now = now.tz_convert(cfg["timezone"])

    times = pd.date_range(
        end=now,
        periods=window_minutes,
        freq="1min",
        tz=cfg["timezone"],
    )

    irr_sum = np.zeros(len(df))
    shadow_sum = np.zeros(len(df))
    base_ghi_values = []
    clear_sky_values = []

    for t in times:
        irr, shadow, base_ghi, clear_sky_ghi = compute_effective_irradiance(
            df,
            location,
            t,
            obstacles_metric,
            obstacle_sindex,
            weather_cache,
            cfg,
        )
        irr_sum += irr
        shadow_sum += shadow
        base_ghi_values.append(base_ghi)
        clear_sky_values.append(clear_sky_ghi)

    avg_irr = irr_sum / max(len(times), 1)
    avg_shadow = shadow_sum / max(len(times), 1)
    avg_power = avg_irr * SOLAR_PANEL_AREA_M2 * PANEL_EFFICIENCY

    return {
        "times": times,
        "irradiance": avg_irr,
        "shadow_factor": avg_shadow,
        "power": avg_power,
        "base_ghi_mean": float(np.mean(base_ghi_values)),
        "clear_sky_ghi_mean": float(np.mean(clear_sky_values)),
    }


def add_live_columns(df, result):
    df = df.copy()
    df["live_avg_irradiance_wm2"] = result["irradiance"]
    df["live_power_w"] = result["power"]
    df["live_shadow_factor"] = result["shadow_factor"]

    max_irr = max(float(df["live_avg_irradiance_wm2"].max()), 1.0)
    df["solar_score"] = (
        0.7 * (df["live_avg_irradiance_wm2"] / max_irr)
        + 0.3 * df["live_shadow_factor"]
    )

    return df


def save_live_outputs(df, cfg, now, top_segments):
    OUTPUT_PLOTS.mkdir(parents=True, exist_ok=True)
    prefix = cfg["output_prefix"]

    values_path = OUTPUT_PLOTS / f"{prefix}_live_dashboard_values.csv"
    ideal_path = OUTPUT_PLOTS / f"{prefix}_live_ideal_line_suggestions.csv"
    notes_path = OUTPUT_PLOTS / f"{prefix}_live_ideal_line_notes.txt"

    df.to_csv(values_path, index=False)

    ideal = (
        df.sort_values("solar_score", ascending=False)
        .head(top_segments)
        [[
            "point_id",
            "sector",
            "distance_m",
            "distance_km",
            "segment_center_lon",
            "segment_center_lat",
            "live_avg_irradiance_wm2",
            "live_power_w",
            "live_shadow_factor",
            "solar_score",
        ]]
    )
    ideal.to_csv(ideal_path, index=False)

    best_sector = df.groupby("sector", observed=False)["solar_score"].mean().idxmax()
    worst_sector = df.groupby("sector", observed=False)["solar_score"].mean().idxmin()

    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("Live solar ideal line suggestions\n")
        f.write("================================\n\n")
        f.write(f"Updated at: {now}\n")
        f.write(f"Track: {cfg['name']}\n\n")
        f.write(f"Best average sector right now: {best_sector}\n")
        f.write(f"Worst average sector right now: {worst_sector}\n\n")
        f.write("Interpretation:\n")
        f.write("- high irradiance = more useful solar input\n")
        f.write("- high shadow factor = less shadow penalty\n")
        f.write("- high solar_score = segment is currently solar-friendly\n\n")
        f.write("Important: this suggests solar-friendly segments, not a full racing-line optimizer.\n")

    return values_path, ideal_path, notes_path


def sector_summary(df):
    return (
        df.groupby("sector", observed=False)
        .agg(
            avg_irradiance=("live_avg_irradiance_wm2", "mean"),
            avg_power=("live_power_w", "mean"),
            avg_shadow=("live_shadow_factor", "mean"),
            best_score=("solar_score", "max"),
        )
        .reset_index()
    )


def setup_dashboard(df, cfg):
    plt.ion()
    fig = plt.figure(figsize=(18, 9))
    gs = fig.add_gridspec(2, 3)

    ax_map = fig.add_subplot(gs[:, 0])
    ax_ideal = fig.add_subplot(gs[:, 1])
    ax_compare = fig.add_subplot(gs[0, 2])
    ax_sector = fig.add_subplot(gs[1, 2])

    scatter = ax_map.scatter(
        df["segment_center_lon"],
        df["segment_center_lat"],
        c=np.zeros(len(df)),
        s=35,
    )
    cbar = fig.colorbar(scatter, ax=ax_map)
    cbar.set_label("15-min avg effective irradiance (W/m²)")

    car_marker = ax_map.scatter(
        [],
        [],
        s=140,
        marker="o",
        edgecolors="black",
        linewidths=1.5,
        zorder=20,
        label="Car GPS",
    )
    ax_map.legend(loc="best")

    ax_map.set_xlabel("Longitude")
    ax_map.set_ylabel("Latitude")
    ax_map.set_aspect("equal")

    ax_compare.set_xlabel("Timestamp")
    ax_compare.set_ylabel("Power (W)")
    ax_compare.set_title("Calculated vs measured solar power")
    ax_compare.grid(True)

    ax_sector.set_xlabel("Sector")
    ax_sector.set_ylabel("Avg irradiance (W/m²)")
    ax_sector.grid(axis="y")

    ax_ideal.set_xlabel("Longitude")
    ax_ideal.set_ylabel("Latitude")
    ax_ideal.set_aspect("equal")
    ax_ideal.set_title("Ideal racing line")

    fig.suptitle(f"Solar Dashboard + Influx Replay - {cfg['name']}")
    fig.tight_layout()

    return (
        fig,
        ax_map,
        ax_ideal,
        ax_compare,
        ax_sector,
        scatter,
        car_marker,
    )


def update_dashboard(
    fig,
    ax_map,
    ax_ideal,
    ax_compare,
    ax_sector,
    scatter,
    car_marker,
    df_live,
    comparison_history,
    cfg,
    now,
    weather_source,
    ideal_result=None,
    gps_lat=None,
    gps_lon=None,
    measured_power=None,
    calculated_power=None,
):
    values = df_live["live_avg_irradiance_wm2"].to_numpy()
    scatter.set_array(values)
    scatter.set_clim(
        vmin=float(np.min(values)),
        vmax=max(float(np.max(values)), 1.0),
    )

    if (
        gps_lat is not None
        and gps_lon is not None
        and np.isfinite(gps_lat)
        and np.isfinite(gps_lon)
    ):
        car_marker.set_offsets([[gps_lon, gps_lat]])
        car_marker.set_visible(True)
    else:
        car_marker.set_visible(False)

    best_row = df_live.sort_values(
        "solar_score",
        ascending=False,
    ).iloc[0]

    title = (
        f"{cfg['name']} heatmap | "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"weather: {weather_source}\n"
        f"Best segment: point {int(best_row['point_id'])}, "
        f"{best_row['sector']}, "
        f"{best_row['distance_km']:.2f} km"
    )

    if measured_power is not None and calculated_power is not None:
        title += (
            f" | model={calculated_power:.1f} W"
            f" | MPPT={measured_power:.1f} W"
        )

    ax_map.set_title(title)

    ax_ideal.clear()
    ax_ideal.set_xlabel("Longitude")
    ax_ideal.set_ylabel("Latitude")
    ax_ideal.set_aspect("equal")

    if ideal_result is not None and ideal_result.get("available"):
        candidates = ideal_result["candidates"]
        ideal_df = ideal_result["ideal_df"]

        ax_ideal.scatter(
            candidates["lon"],
            candidates["lat"],
            c=candidates["candidate_irradiance_wm2"],
            s=8,
        )
        ax_ideal.plot(
            ideal_df["lon"],
            ideal_df["lat"],
            linewidth=3,
            color="crimson",
            zorder=15,
            label="Ideal racing line",
        )
        ax_ideal.scatter(
            ideal_df["lon"].iloc[0],
            ideal_df["lat"].iloc[0],
            s=45,
            marker="o",
        )

        if gps_lat is not None and gps_lon is not None:
            ax_ideal.scatter(
                [gps_lon],
                [gps_lat],
                s=100,
                marker="o",
                edgecolors="black",
                linewidths=1.2,
                zorder=20,
            )

        best_lane = ideal_df.sort_values(
            "solar_score",
            ascending=False,
        ).iloc[0]

        ax_ideal.set_title(
            "Solar racing line\n"
            f"best lane point {int(best_lane['point_id'])}, "
            f"lane {best_lane['lane_alpha']:.2f}"
        )
        ax_ideal.legend(loc="best")
    elif ideal_result is not None:
        ax_ideal.scatter(
            df_live["segment_center_lon"],
            df_live["segment_center_lat"],
            c=df_live["live_avg_irradiance_wm2"],
            s=18,
        )
        if gps_lat is not None and gps_lon is not None:
            ax_ideal.scatter(
                [gps_lon],
                [gps_lat],
                s=100,
                marker="o",
                edgecolors="black",
                zorder=20,
            )
        ax_ideal.set_title(
            "No boundary file found\n"
            "save <track>_track_boundaries.geojson"
        )
    else:
        ax_ideal.scatter(
            df_live["segment_center_lon"],
            df_live["segment_center_lat"],
            c=df_live["live_avg_irradiance_wm2"],
            s=18,
        )
        if gps_lat is not None and gps_lon is not None:
            ax_ideal.scatter(
                [gps_lon],
                [gps_lat],
                s=100,
                marker="o",
                edgecolors="black",
                zorder=20,
            )
        ax_ideal.set_title("Ideal racing line disabled")

    track_lon = df_live["segment_center_lon"].to_numpy(dtype=float)
    track_lat = df_live["segment_center_lat"].to_numpy(dtype=float)
    lon_pad = max(float(np.ptp(track_lon)) * 0.08, 0.0001)
    lat_pad = max(float(np.ptp(track_lat)) * 0.08, 0.0001)
    ax_ideal.set_xlim(float(np.min(track_lon)) - lon_pad, float(np.max(track_lon)) + lon_pad)
    ax_ideal.set_ylim(float(np.min(track_lat)) - lat_pad, float(np.max(track_lat)) + lat_pad)

    ax_compare.clear()
    ax_compare.grid(True)
    ax_compare.set_xlabel("Timestamp")
    ax_compare.set_ylabel("Power (W)")
    ax_compare.set_title("Solar model vs measured MPPT power")

    if comparison_history:
        times = [row["time"] for row in comparison_history]
        calculated = [row["calculated"] for row in comparison_history]
        measured = [row["measured"] for row in comparison_history]

        ax_compare.plot(
            times,
            calculated,
            marker="o",
            label="Calculated solar power",
        )
        ax_compare.plot(
            times,
            measured,
            marker="o",
            label="Measured MPPT power",
        )
        ax_compare.legend()
        ax_compare.tick_params(axis="x", rotation=30)

    ax_sector.clear()
    ax_sector.grid(axis="y")
    summary = sector_summary(df_live)
    ax_sector.bar(
        summary["sector"].astype(str),
        summary["avg_irradiance"],
    )
    ax_sector.set_xlabel("Sector")
    ax_sector.set_ylabel("Avg irradiance (W/m²)")
    ax_sector.set_title("15-min average by sector")

    fig.canvas.draw_idle()
    plt.pause(0.1)


def run_dashboard(args):
    if args.live_influx and args.comparison_csv:
        raise ValueError("--live-influx and --comparison-csv cannot be used together.")

    catalog = build_track_catalog()
    track_key, cfg = choose_track(catalog, args.track)

    df, cfg = load_track(cfg)
    obstacles_metric = load_obstacles(cfg["obstacles_geojson"])
    obstacle_sindex = (
        obstacles_metric.sindex
        if not obstacles_metric.empty
        else None
    )

    location = Location(
        cfg["lat"],
        cfg["lon"],
        tz=cfg["timezone"],
    )
    weather_cache = WeatherCache(
        enabled=not args.no_weather_api
    )

    comparison_df = None
    if args.comparison_csv:
        comparison_path = Path(args.comparison_csv)
        if not comparison_path.exists():
            raise FileNotFoundError(
                f"Comparison CSV not found: {comparison_path}"
            )

        comparison_df = pd.read_csv(comparison_path)
        comparison_df["timestamp_utc"] = pd.to_datetime(
            comparison_df["timestamp_utc"],
            utc=True,
            errors="coerce",
        )
        comparison_df = comparison_df.dropna(
            subset=[
                "timestamp_utc",
                "gps_latitude",
                "gps_longitude",
                "calculated_solar_power_w",
                "measured_solar_power_w",
            ]
        ).sort_values("timestamp_utc").reset_index(drop=True)

        if comparison_df.empty:
            raise ValueError("Comparison CSV contains no usable rows.")

    influx_client = (
        InfluxGPSClient.from_env()
        if args.live_influx
        else None
    )

    print("\nDashboard started")
    print(f"Track: {track_key} / {cfg['name']}")
    print(
        "Coordinates used for weather/solar: "
        f"lat={cfg['lat']:.5f}, lon={cfg['lon']:.5f}"
    )
    print(f"Rolling window: {args.window_minutes} minutes")

    if args.live_influx:
        print(
            "Telemetry mode: live InfluxDB | "
            f"maximum age={args.influx_max_age_seconds}s"
        )
        print(f"Update every: {args.update_seconds} seconds")
    elif comparison_df is not None:
        print(
            f"Historical replay rows: {len(comparison_df)} | "
            f"delay={args.replay_delay_seconds}s"
        )
        print(
            "Weather mode: Open-Meteo Historical Weather API "
            "for replay timestamps"
        )
    else:
        print(f"Update every: {args.update_seconds} seconds")
        print("Weather mode: Open-Meteo Forecast API")

    print("Close the plot window or press Ctrl+C to stop.\n")

    (
        fig,
        ax_map,
        ax_ideal,
        ax_compare,
        ax_sector,
        scatter,
        car_marker,
    ) = setup_dashboard(df, cfg)

    comparison_history = deque(maxlen=120)
    replay_index = 0

    while plt.fignum_exists(fig.number):
        if comparison_df is not None:
            sample = comparison_df.iloc[replay_index]

            now = sample["timestamp_utc"].tz_convert(
                cfg["timezone"]
            )
            gps_lat = float(sample["gps_latitude"])
            gps_lon = float(sample["gps_longitude"])
            measured_power = float(
                sample["measured_solar_power_w"]
            )
            calculated_power = float(
                sample["calculated_solar_power_w"]
            )
        elif influx_client is not None:
            now = pd.Timestamp.now(tz=cfg["timezone"])
            gps_lat = None
            gps_lon = None
            measured_power = None
            calculated_power = None

            try:
                telemetry = influx_client.get_latest_telemetry()
                gps_time = pd.Timestamp(telemetry.gps.timestamp)
                mppt_time = pd.Timestamp(telemetry.mppt.timestamp)
                if gps_time.tzinfo is None:
                    gps_time = gps_time.tz_localize("UTC")
                if mppt_time.tzinfo is None:
                    mppt_time = mppt_time.tz_localize("UTC")

                now_utc = pd.Timestamp.now(tz="UTC")
                gps_age = (now_utc - gps_time.tz_convert("UTC")).total_seconds()
                mppt_age = (now_utc - mppt_time.tz_convert("UTC")).total_seconds()

                if gps_age <= args.influx_max_age_seconds:
                    gps_lat = telemetry.gps.latitude
                    gps_lon = telemetry.gps.longitude
                else:
                    print(
                        f"[influx] GPS data is stale ({gps_age:.1f}s old); "
                        "hiding the car marker."
                    )

                if (
                    mppt_age <= args.influx_max_age_seconds
                    and telemetry.mppt.measured_power_w is not None
                ):
                    measured_power = telemetry.mppt.measured_power_w
                else:
                    print(
                        f"[influx] MPPT data unavailable or stale "
                        f"({mppt_age:.1f}s old)."
                    )
            except Exception as exc:
                print(f"[influx] Live telemetry unavailable: {exc}")
        else:
            now = pd.Timestamp.now(tz=cfg["timezone"])
            gps_lat = None
            gps_lon = None
            measured_power = None
            calculated_power = None

        if (
            gps_lat is not None
            and gps_lon is not None
            and not gps_is_valid_for_track(gps_lat, gps_lon, df)
        ):
            print(
                f"[gps] Ignoring invalid/off-track position "
                f"({gps_lat:.6f}, {gps_lon:.6f})."
            )
            gps_lat = None
            gps_lon = None

        result = compute_rolling_window(
            df,
            location,
            now,
            args.window_minutes,
            obstacles_metric,
            obstacle_sindex,
            weather_cache,
            cfg,
        )

        df_live = add_live_columns(df, result)
        _, ideal_path, _ = save_live_outputs(
            df_live,
            cfg,
            now,
            args.top_segments,
        )

        avg_irr = float(
            df_live["live_avg_irradiance_wm2"].mean()
        )
        avg_power = float(df_live["live_power_w"].mean())
        if influx_client is not None:
            calculated_power = avg_power

        if (
            measured_power is not None
            and calculated_power is not None
        ):
            comparison_history.append({
                "time": now,
                "calculated": calculated_power,
                "measured": measured_power,
            })

        ideal_result = None
        if not args.no_racing_line:
            try:
                ideal_result = compute_ideal_racing_line(
                    df=df_live,
                    cfg=cfg,
                    timestamp=now,
                    obstacles_metric=obstacles_metric,
                    obstacle_sindex=obstacle_sindex,
                    weather_cache=weather_cache,
                    boundary_path=args.boundary_file,
                    save_outputs=True,
                )
            except Exception as exc:
                print(
                    "[ideal-line] Could not compute "
                    f"ideal racing line: {exc}"
                )
                ideal_result = {
                    "available": False,
                    "reason": str(exc),
                }

        update_dashboard(
            fig=fig,
            ax_map=ax_map,
            ax_ideal=ax_ideal,
            ax_compare=ax_compare,
            ax_sector=ax_sector,
            scatter=scatter,
            car_marker=car_marker,
            df_live=df_live,
            comparison_history=comparison_history,
            cfg=cfg,
            now=now,
            weather_source=weather_cache.source,
            ideal_result=ideal_result,
            gps_lat=gps_lat,
            gps_lon=gps_lon,
            measured_power=measured_power,
            calculated_power=calculated_power,
        )

        message = (
            f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"avg irradiance={avg_irr:.1f} W/m² | "
            f"avg power={avg_power:.1f} W | "
            f"weather={weather_cache.source}"
        )

        if (
            measured_power is not None
            and calculated_power is not None
        ):
            message += (
                f" | model={calculated_power:.1f} W"
                f" | MPPT={measured_power:.1f} W"
            )
        if gps_lat is not None and gps_lon is not None:
            message += f" | GPS=({gps_lat:.6f}, {gps_lon:.6f})"

        print(message)

        if comparison_df is not None:
            replay_index += 1

            if replay_index >= len(comparison_df):
                if args.loop_replay:
                    replay_index = 0
                    comparison_history.clear()
                else:
                    print("Historical replay finished.")
                    while plt.fignum_exists(fig.number):
                        plt.pause(1)
                    break

            wait_seconds = max(
                args.replay_delay_seconds,
                0.1,
            )
        else:
            wait_seconds = max(args.update_seconds, 1)

        elapsed = 0.0
        while (
            elapsed < wait_seconds
            and plt.fignum_exists(fig.number)
        ):
            step = min(0.25, wait_seconds - elapsed)
            plt.pause(step)
            elapsed += step

    print("Dashboard closed.")


def parse_args():
    parser = argparse.ArgumentParser(description="Live solar heatmap dashboard")
    parser.add_argument(
        "--track",
        choices=list(CONFIG_TRACKS.keys()),
        default=None,
        help="Track key from config.py. If omitted, you choose at runtime.",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=DEFAULT_WINDOW_MINUTES,
        help="Rolling average window in minutes.",
    )
    parser.add_argument(
        "--update-seconds",
        type=int,
        default=DEFAULT_UPDATE_SECONDS,
        help="Dashboard refresh interval in seconds.",
    )
    parser.add_argument(
        "--top-segments",
        type=int,
        default=DEFAULT_TOP_SEGMENTS,
        help="How many best solar segments to export.",
    )
    parser.add_argument(
        "--no-weather-api",
        action="store_true",
        help="Disable Open-Meteo and use only pvlib clear-sky.",
    )
    parser.add_argument(
        "--no-racing-line",
        action="store_true",
        help="Disable the extra ideal racing line map.",
    )
    parser.add_argument(
        "--boundary-file",
        default=None,
        help="Optional custom GeoJSON with two track boundary LineStrings.",
    )
    parser.add_argument(
        "--comparison-csv",
        default=None,
        help=(
            "Historical comparison CSV generated by "
            "tools/compare_historical_solar.py. When provided, "
            "the dashboard replays GPS and MPPT data."
        ),
    )
    parser.add_argument(
        "--live-influx",
        action="store_true",
        help="Read the latest GPS and MPPT telemetry directly from InfluxDB.",
    )
    parser.add_argument(
        "--influx-max-age-seconds",
        type=float,
        default=30.0,
        help="Reject live GPS/MPPT samples older than this many seconds.",
    )
    parser.add_argument(
        "--replay-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between historical replay samples.",
    )
    parser.add_argument(
        "--loop-replay",
        action="store_true",
        help="Restart historical replay after the last row.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        run_dashboard(parse_args())
    except KeyboardInterrupt:
        print("\nStopped by user.")
