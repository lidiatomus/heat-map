

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import urlopen

import geopandas as gpd
import numpy as np
import pandas as pd
from pvlib.location import Location
from shapely.geometry import LineString

from config import METRIC_CRS, TRACKS as CONFIG_TRACKS
from influx_gps import InfluxGPSClient


# -----------------------------
# Solar / car constants
# -----------------------------
# Keep these here so the twin can override them without touching the old scripts.
DEFAULT_SOLAR_PANEL_AREA_M2 = 4.0
DEFAULT_PANEL_EFFICIENCY = 0.24
DEFAULT_DIRECTION_WEIGHT = 0.1
DEFAULT_RAY_LENGTH_M = 20.0
DEFAULT_SHADOW_MIN_FACTOR = 0.65
DEFAULT_USE_DAYLIGHT_ONLY = True

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@dataclass
class SolarModelConfig:
    solar_panel_area_m2: float = DEFAULT_SOLAR_PANEL_AREA_M2
    panel_efficiency: float = DEFAULT_PANEL_EFFICIENCY
    direction_weight: float = DEFAULT_DIRECTION_WEIGHT
    ray_length_m: float = DEFAULT_RAY_LENGTH_M
    shadow_min_factor: float = DEFAULT_SHADOW_MIN_FACTOR
    use_daylight_only: bool = DEFAULT_USE_DAYLIGHT_ONLY
    weather_refresh_seconds: int = 3600


class WeatherCache:
    """Small Open-Meteo cache with clean fallback to pvlib clear-sky."""

    def __init__(self, enabled: bool = True, refresh_seconds: int = 3600):
        self.enabled = enabled
        self.refresh_seconds = refresh_seconds
        self.last_fetch_ts: float = 0.0
        self.df: Optional[pd.DataFrame] = None
        self.source: str = "clear-sky"
        self.last_error: str = ""
        self.cache_key: Optional[Tuple[float, float, str]] = None

    def fetch_if_needed(
        self, lat: float, lon: float, timezone: str, timestamp: Any
    ) -> Optional[pd.DataFrame]:
        if not self.enabled:
            self.source = "clear-sky"
            return None

        target = normalize_timestamp(timestamp, timezone)
        now_local = pd.Timestamp.now(tz=timezone)
        is_historical = target.date() < (now_local - pd.Timedelta(days=5)).date()

        if is_historical:
            start_date = (target - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            end_date = (target + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            key = (
                "historical", start_date, end_date,
                round(float(lat), 5), round(float(lon), 5), str(timezone),
            )
            endpoint = OPEN_METEO_ARCHIVE_URL
            params = {
                "latitude": lat,
                "longitude": lon,
                "timezone": timezone,
                "start_date": start_date,
                "end_date": end_date,
                "hourly": ",".join([
                    "shortwave_radiation", "direct_radiation",
                    "diffuse_radiation", "cloud_cover",
                ]),
            }
            source_name = "Open-Meteo historical"
        else:
            key = (
                "forecast", target.strftime("%Y-%m-%d"),
                round(float(lat), 5), round(float(lon), 5), str(timezone),
            )
            endpoint = OPEN_METEO_URL
            params = {
                "latitude": lat,
                "longitude": lon,
                "timezone": timezone,
                "forecast_days": 2,
                "hourly": ",".join([
                    "shortwave_radiation", "direct_radiation",
                    "diffuse_radiation", "cloud_cover",
                ]),
            }
        now = time.time()

        if (
            self.df is not None
            and self.cache_key == key
            and now - self.last_fetch_ts < self.refresh_seconds
        ):
            return self.df

        url = endpoint + "?" + urlencode(params)

        try:
            with urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            if not times:
                raise ValueError("Open-Meteo response has no hourly time values.")

            weather_df = pd.DataFrame(
                {
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
                    "cloud_cover": hourly.get("cloud_cover", [np.nan] * len(times)),
                }
            )

            self.df = weather_df
            self.last_fetch_ts = now
            self.cache_key = key
            self.source = source_name
            self.last_error = ""
            return self.df

        except Exception as exc:  # noqa: BLE001 - fallback is intentional here
            self.df = None
            self.cache_key = key
            self.source = "clear-sky fallback"
            self.last_error = str(exc)
            return None

    def weather_base_ghi(
        self,
        timestamp: pd.Timestamp,
        clear_sky_ghi: float,
        lat: float,
        lon: float,
        timezone: str,
    ) -> float:
        """Return real-weather GHI when available, otherwise clear-sky GHI."""
        weather_df = self.fetch_if_needed(lat, lon, timezone, timestamp)

        if weather_df is None or weather_df.empty:
            return float(max(clear_sky_ghi, 0.0))

        target = normalize_timestamp(timestamp, timezone)
        ts_seconds = target.timestamp()
        weather_seconds = weather_df["time"].map(lambda x: x.timestamp()).to_numpy()

        if (
            len(weather_seconds) == 0
            or ts_seconds < weather_seconds.min()
            or ts_seconds > weather_seconds.max()
        ):
            self.source = "clear-sky historical fallback"
            return float(max(clear_sky_ghi, 0.0))

        shortwave = pd.to_numeric(
            weather_df["shortwave_radiation"],
            errors="coerce",
        ).to_numpy(dtype=float)

        shortwave = pd.to_numeric(
            weather_df["shortwave_radiation"], errors="coerce"
        ).to_numpy(dtype=float)
        cloud = pd.to_numeric(weather_df["cloud_cover"], errors="coerce").to_numpy(
            dtype=float
        )

        # Best case: Open-Meteo already gives shortwave radiation.
        if len(weather_seconds) >= 2 and np.isfinite(shortwave).any():
            valid = np.isfinite(shortwave)
            weather_ghi = np.interp(ts_seconds, weather_seconds[valid], shortwave[valid])
            if np.isfinite(weather_ghi):
                return float(max(weather_ghi, 0.0))

        # Fallback inside weather mode: attenuate clear-sky with cloud cover.
        if len(weather_seconds) >= 2 and np.isfinite(cloud).any():
            valid = np.isfinite(cloud)
            cloud_value = np.interp(ts_seconds, weather_seconds[valid], cloud[valid])
            cloud_factor = 1.0 - 0.75 * np.clip(cloud_value, 0, 100) / 100.0
            cloud_factor = np.clip(cloud_factor, 0.15, 1.0)
            return float(max(clear_sky_ghi * cloud_factor, 0.0))

        return float(max(clear_sky_ghi, 0.0))


def normalize_timestamp(timestamp: Any, timezone: str) -> pd.Timestamp:
    """Normalize timestamp strings / pandas timestamps / 'now' to track timezone."""
    if timestamp is None or str(timestamp).lower() == "now":
        ts = pd.Timestamp.now(tz=timezone)
    else:
        ts = pd.Timestamp(timestamp)

    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone)
    else:
        ts = ts.tz_convert(timezone)

    return ts


def _validate_track_dataframe(df: pd.DataFrame) -> None:
    required = ["point_id", "lon", "lat", "distance_m", "heading_deg", "slope_deg"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(
            f"Track CSV is missing columns {missing}. Run 02_sample_evaluation.py first."
        )


class SolarModel:
    """
    Reusable solar model for the digital twin.

    It loads track points, heading, slope/pitch, obstacles and optional weather.
    It returns solar input only: irradiance and power.
    """

    def __init__(
        self,
        use_weather_api: bool = True,
        model_config: Optional[SolarModelConfig] = None,
    ):
        self.model_config = model_config or SolarModelConfig()
        self.weather_cache = WeatherCache(
            enabled=use_weather_api,
            refresh_seconds=self.model_config.weather_refresh_seconds,
        )
        self._track_cache: Dict[str, Tuple[pd.DataFrame, Dict[str, Any]]] = {}
        self._obstacle_cache: Dict[str, Tuple[gpd.GeoDataFrame, Any]] = {}

    # -----------------------------
    # Loading helpers
    # -----------------------------
    def _base_track_cfg(self, track: str) -> Dict[str, Any]:
        if track not in CONFIG_TRACKS:
            raise ValueError(f"Unknown track '{track}'. Available: {list(CONFIG_TRACKS)}")

        cfg = CONFIG_TRACKS[track].copy()
        cfg.setdefault("output_prefix", track)
        cfg.setdefault("name", track)
        return cfg

    def load_track(self, track: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Load and cache track_3d CSV with metric coordinates."""
        if track in self._track_cache:
            return self._track_cache[track]

        cfg = self._base_track_cfg(track)
        path = cfg.get("track_3d_csv")
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"Missing track file for '{track}': {path}. Run 02_sample_evaluation.py first."
            )

        df = pd.read_csv(path)
        _validate_track_dataframe(df)
        df = df.copy()

        df["segment_length_m"] = df["distance_m"].diff().bfill().fillna(0)
        df["segment_center_lon"] = ((df["lon"] + df["lon"].shift(-1)) / 2).fillna(
            df["lon"]
        )
        df["segment_center_lat"] = ((df["lat"] + df["lat"].shift(-1)) / 2).fillna(
            df["lat"]
        )

        track_length = float(df["distance_m"].max())
        if track_length > 0:
            df["sector"] = pd.cut(
                df["distance_m"],
                bins=[0, track_length / 3, 2 * track_length / 3, track_length],
                labels=["Sector 1", "Sector 2", "Sector 3"],
                include_lowest=True,
            )
        else:
            df["sector"] = "Sector 1"

        track_points = gpd.GeoDataFrame(
            df,
            geometry=gpd.points_from_xy(df["lon"], df["lat"]),
            crs="EPSG:4326",
        ).to_crs(METRIC_CRS)

        df["x_m"] = track_points.geometry.x
        df["y_m"] = track_points.geometry.y

        # Use actual track center for weather. This matters for the Bucharest demo track.
        cfg["lat"] = float(df["lat"].mean())
        cfg["lon"] = float(df["lon"].mean())

        self._track_cache[track] = (df, cfg)
        return df, cfg

    def load_obstacles(self, track: str) -> Tuple[gpd.GeoDataFrame, Any]:
        """Load and cache obstacle GeoJSON + spatial index."""
        if track in self._obstacle_cache:
            return self._obstacle_cache[track]

        cfg = self._base_track_cfg(track)
        path = cfg.get("obstacles_geojson")

        if path is not None and os.path.exists(path):
            obstacles = gpd.read_file(path).to_crs(METRIC_CRS)
        else:
            obstacles = gpd.GeoDataFrame(geometry=[], crs=METRIC_CRS)

        obstacle_sindex = obstacles.sindex if not obstacles.empty else None
        self._obstacle_cache[track] = (obstacles, obstacle_sindex)
        return obstacles, obstacle_sindex

    # -----------------------------
    # Track lookup helpers
    # -----------------------------
    def nearest_point_id(
        self,
        track: str,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        distance_m: Optional[float] = None,
    ) -> int:
        """Find nearest track point by lat/lon or by distance along track."""
        df, _ = self.load_track(track)

        if distance_m is not None:
            idx = (df["distance_m"] - float(distance_m)).abs().idxmin()
            return int(df.loc[idx, "point_id"])

        if lat is None or lon is None:
            raise ValueError("Provide either distance_m or both lat and lon.")

        point = gpd.GeoDataFrame(
            {"tmp": [1]},
            geometry=gpd.points_from_xy([float(lon)], [float(lat)]),
            crs="EPSG:4326",
        ).to_crs(METRIC_CRS)

        px = float(point.geometry.x.iloc[0])
        py = float(point.geometry.y.iloc[0])
        dist2 = (df["x_m"] - px) ** 2 + (df["y_m"] - py) ** 2
        idx = dist2.idxmin()
        return int(df.loc[idx, "point_id"])

    # -----------------------------
    # Physics / solar calculation
    # -----------------------------
    def _compute_shadow_factor_for_rows(
        self,
        rows: pd.DataFrame,
        obstacles_metric: gpd.GeoDataFrame,
        obstacle_sindex: Any,
        solar_azimuth: np.ndarray,
    ) -> np.ndarray:
        cfg = self.model_config
        shadow_factor = np.ones(len(rows))

        if obstacles_metric.empty or obstacle_sindex is None:
            return shadow_factor

        for out_idx, (_, row) in enumerate(rows.iterrows()):
            x = float(row["x_m"])
            y = float(row["y_m"])
            az = float(solar_azimuth[out_idx])

            dx = -cfg.ray_length_m * np.sin(np.radians(az))
            dy = -cfg.ray_length_m * np.cos(np.radians(az))
            ray = LineString([(x, y), (x + dx, y + dy)])

            possible_idx = list(obstacle_sindex.intersection(ray.bounds))
            if not possible_idx:
                continue

            candidates = obstacles_metric.iloc[possible_idx]
            hits = candidates[candidates.intersects(ray)]

            if len(hits) > 0:
                distances = hits.distance(ray.centroid)
                min_distance = float(distances.min())
                value = np.interp(
                    min_distance,
                    [0, cfg.ray_length_m],
                    [cfg.shadow_min_factor, 1.0],
                )
                shadow_factor[out_idx] = np.clip(
                    value, cfg.shadow_min_factor, 1.0
                )

        return shadow_factor

    def _compute_solar_for_rows(
        self,
        track: str,
        rows: pd.DataFrame,
        timestamp: Any,
        heading_deg_override: Optional[float] = None,
        slope_deg_override: Optional[float] = None,
    ) -> pd.DataFrame:
        """Compute irradiance/power for any set of track rows."""
        full_df, cfg = self.load_track(track)
        _ = full_df  # keeps intent clear; cfg comes from the cached track
        obstacles, obstacle_sindex = self.load_obstacles(track)
        mc = self.model_config

        ts = normalize_timestamp(timestamp, cfg["timezone"])
        point_times = pd.DatetimeIndex([ts] * len(rows))
        location = Location(cfg["lat"], cfg["lon"], tz=cfg["timezone"])

        clear_sky = location.get_clearsky(point_times)
        solar_position = location.get_solarposition(point_times)

        clear_sky_ghi = float(clear_sky["ghi"].iloc[0])
        base_ghi = self.weather_cache.weather_base_ghi(
            timestamp=ts,
            clear_sky_ghi=clear_sky_ghi,
            lat=cfg["lat"],
            lon=cfg["lon"],
            timezone=cfg["timezone"],
        )

        solar_azimuth = solar_position["azimuth"].to_numpy(dtype=float)
        solar_zenith = solar_position["zenith"].to_numpy(dtype=float)

        if heading_deg_override is None:
            heading_deg = rows["heading_deg"].to_numpy(dtype=float)
        else:
            heading_deg = np.full(len(rows), float(heading_deg_override))

        if slope_deg_override is None:
            slope_deg = rows["slope_deg"].to_numpy(dtype=float)
        else:
            slope_deg = np.full(len(rows), float(slope_deg_override))

        angle_diff = np.abs(solar_azimuth - heading_deg)
        angle_diff = np.minimum(angle_diff, 360 - angle_diff)

        orientation_factor = np.cos(np.radians(angle_diff))
        orientation_factor = np.maximum(orientation_factor, 0)

        zenith_factor = np.cos(np.radians(solar_zenith))
        zenith_factor = np.maximum(zenith_factor, 0)

        pitch_factor = np.cos(np.radians(slope_deg))
        pitch_factor = np.maximum(pitch_factor, 0.85)

        shadow_factor = self._compute_shadow_factor_for_rows(
            rows,
            obstacles,
            obstacle_sindex,
            solar_azimuth,
        )

        effective_irradiance = base_ghi * (0.9 + mc.direction_weight * orientation_factor)
        effective_irradiance *= zenith_factor
        effective_irradiance *= pitch_factor
        effective_irradiance *= shadow_factor

        if mc.use_daylight_only:
            effective_irradiance[solar_zenith >= 90] = 0

        effective_irradiance = np.maximum(effective_irradiance, 0)
        solar_power = effective_irradiance * mc.solar_panel_area_m2 * mc.panel_efficiency

        result = rows.copy()
        result["timestamp"] = str(ts)
        result["base_ghi_wm2"] = base_ghi
        result["clear_sky_ghi_wm2"] = clear_sky_ghi
        result["solar_azimuth_deg"] = solar_azimuth
        result["solar_zenith_deg"] = solar_zenith
        result["orientation_factor"] = orientation_factor
        result["pitch_factor"] = pitch_factor
        result["shadow_factor"] = shadow_factor
        result["irradiance_wm2"] = effective_irradiance
        result["solar_power_w"] = solar_power
        result["weather_source"] = self.weather_cache.source
        result["weather_error"] = self.weather_cache.last_error
        result["panel_area_m2"] = mc.solar_panel_area_m2
        result["panel_efficiency"] = mc.panel_efficiency

        return result

    # -----------------------------
    # Public API for the twin
    # -----------------------------
    def get_solar_input(
        self,
        track: str,
        timestamp: Any = "now",
        point_id: Optional[int] = None,
        distance_m: Optional[float] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        heading_deg: Optional[float] = None,
        slope_deg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Return solar input for one track point / car position.

        You can specify the position using one of:
        - point_id
        - distance_m along the track
        - lat + lon, which is snapped to the nearest track point

        Optional heading_deg and slope_deg can override the track values when the
        twin has live vehicle pose data.
        """
        df, cfg = self.load_track(track)

        if point_id is None:
            point_id = self.nearest_point_id(
                track=track,
                lat=lat,
                lon=lon,
                distance_m=distance_m,
            )

        rows = df[df["point_id"] == int(point_id)]
        if rows.empty:
            raise ValueError(f"point_id {point_id} not found for track '{track}'.")

        solar_df = self._compute_solar_for_rows(
            track=track,
            rows=rows,
            timestamp=timestamp,
            heading_deg_override=heading_deg,
            slope_deg_override=slope_deg,
        )
        row = solar_df.iloc[0]

        return {
            "track": track,
            "track_name": cfg["name"],
            "point_id": int(row["point_id"]),
            "timestamp": row["timestamp"],
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "distance_m": float(row["distance_m"]),
            "distance_km": float(row.get("distance_km", row["distance_m"] / 1000.0)),
            "sector": str(row.get("sector", "")),
            "heading_deg": float(heading_deg if heading_deg is not None else row["heading_deg"]),
            "slope_deg": float(slope_deg if slope_deg is not None else row["slope_deg"]),
            "irradiance_wm2": float(row["irradiance_wm2"]),
            "solar_power_w": float(row["solar_power_w"]),
            "shadow_factor": float(row["shadow_factor"]),
            "orientation_factor": float(row["orientation_factor"]),
            "pitch_factor": float(row["pitch_factor"]),
            "base_ghi_wm2": float(row["base_ghi_wm2"]),
            "clear_sky_ghi_wm2": float(row["clear_sky_ghi_wm2"]),
            "solar_azimuth_deg": float(row["solar_azimuth_deg"]),
            "solar_zenith_deg": float(row["solar_zenith_deg"]),
            "panel_area_m2": float(row["panel_area_m2"]),
            "panel_efficiency": float(row["panel_efficiency"]),
            "weather_source": str(row["weather_source"]),
            "weather_error": str(row["weather_error"]),
        }

    def get_live_solar_input(
        self,
        track: str,
        gps_client: Optional[InfluxGPSClient] = None,
        heading_deg: Optional[float] = None,
        slope_deg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Read the latest GPS position from InfluxDB and calculate solar input."""
        client = gps_client or InfluxGPSClient.from_env()
        gps = client.get_latest_position()

        result = self.get_solar_input(
            track=track,
            timestamp=gps.timestamp,
            lat=gps.latitude,
            lon=gps.longitude,
            heading_deg=heading_deg,
            slope_deg=slope_deg,
        )
        result.update({
            "gps_latitude": gps.latitude,
            "gps_longitude": gps.longitude,
            "gps_timestamp": str(gps.timestamp),
            "gps_measurement": gps.measurement,
            "gps_ecu": gps.ecu,
            "position_source": "InfluxDB",
        })
        return result

    def get_track_solar_input(self, track: str, timestamp: Any = "now") -> pd.DataFrame:
        """Return solar input for every sampled point on the track at one timestamp."""
        df, _ = self.load_track(track)
        return self._compute_solar_for_rows(track=track, rows=df, timestamp=timestamp)

    def get_track_solar_window(
        self,
        track: str,
        end_time: Any = "now",
        window_minutes: int = 15,
    ) -> pd.DataFrame:
        """
        Return live rolling average solar input for the last N minutes.

        This is the dashboard-friendly version, but it still does not plot anything.
        """
        df, cfg = self.load_track(track)
        end = normalize_timestamp(end_time, cfg["timezone"]).floor("min")
        times = pd.date_range(
            end=end,
            periods=max(int(window_minutes), 1),
            freq="1min",
            tz=cfg["timezone"],
        )

        irr_sum = np.zeros(len(df))
        power_sum = np.zeros(len(df))
        shadow_sum = np.zeros(len(df))
        base_ghi_values = []
        clear_sky_values = []
        weather_sources = []

        for ts in times:
            one = self._compute_solar_for_rows(track=track, rows=df, timestamp=ts)
            irr_sum += one["irradiance_wm2"].to_numpy(dtype=float)
            power_sum += one["solar_power_w"].to_numpy(dtype=float)
            shadow_sum += one["shadow_factor"].to_numpy(dtype=float)
            base_ghi_values.append(float(one["base_ghi_wm2"].iloc[0]))
            clear_sky_values.append(float(one["clear_sky_ghi_wm2"].iloc[0]))
            weather_sources.append(str(one["weather_source"].iloc[0]))

        n = max(len(times), 1)
        out = df.copy()
        out["window_start"] = str(times[0])
        out["window_end"] = str(times[-1])
        out["window_minutes"] = int(window_minutes)
        out["avg_irradiance_wm2"] = irr_sum / n
        out["avg_solar_power_w"] = power_sum / n
        out["avg_shadow_factor"] = shadow_sum / n
        out["base_ghi_mean_wm2"] = float(np.mean(base_ghi_values))
        out["clear_sky_ghi_mean_wm2"] = float(np.mean(clear_sky_values))
        out["weather_source"] = weather_sources[-1] if weather_sources else self.weather_cache.source

        max_irr = max(float(out["avg_irradiance_wm2"].max()), 1.0)
        out["solar_score"] = (
            0.7 * (out["avg_irradiance_wm2"] / max_irr)
            + 0.3 * out["avg_shadow_factor"]
        )
        return out

    def get_best_solar_segment(
        self,
        track: str,
        timestamp: Any = "now",
        window_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return the best current solar segment for debugging/demo."""
        if window_minutes is None:
            df = self.get_track_solar_input(track, timestamp)
            max_irr = max(float(df["irradiance_wm2"].max()), 1.0)
            df["solar_score"] = (
                0.7 * (df["irradiance_wm2"] / max_irr) + 0.3 * df["shadow_factor"]
            )
            score_col = "solar_score"
            irr_col = "irradiance_wm2"
            power_col = "solar_power_w"
            shadow_col = "shadow_factor"
        else:
            df = self.get_track_solar_window(track, timestamp, window_minutes)
            score_col = "solar_score"
            irr_col = "avg_irradiance_wm2"
            power_col = "avg_solar_power_w"
            shadow_col = "avg_shadow_factor"

        row = df.sort_values(score_col, ascending=False).iloc[0]
        return {
            "track": track,
            "point_id": int(row["point_id"]),
            "distance_m": float(row["distance_m"]),
            "distance_km": float(row.get("distance_km", row["distance_m"] / 1000.0)),
            "sector": str(row.get("sector", "")),
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "irradiance_wm2": float(row[irr_col]),
            "solar_power_w": float(row[power_col]),
            "shadow_factor": float(row[shadow_col]),
            "solar_score": float(row[score_col]),
            "weather_source": str(row.get("weather_source", self.weather_cache.source)),
        }


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reusable solar model test CLI.")
    parser.add_argument("--track", default="zolder", choices=list(CONFIG_TRACKS.keys()))
    parser.add_argument("--timestamp", default="now")
    parser.add_argument("--point-id", type=int, default=None)
    parser.add_argument("--distance-m", type=float, default=None)
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--heading-deg", type=float, default=None)
    parser.add_argument("--slope-deg", type=float, default=None)
    parser.add_argument("--no-weather-api", action="store_true")
    parser.add_argument("--window-minutes", type=int, default=None)
    parser.add_argument("--best-segment", action="store_true")
    parser.add_argument("--from-influx", action="store_true", help="Read the latest GPS position from InfluxDB")
    args = parser.parse_args()

    model = SolarModel(use_weather_api=not args.no_weather_api)

    if args.from_influx:
        result = model.get_live_solar_input(
            track=args.track,
            heading_deg=args.heading_deg,
            slope_deg=args.slope_deg,
        )
    elif args.best_segment:
        result = model.get_best_solar_segment(
            track=args.track,
            timestamp=args.timestamp,
            window_minutes=args.window_minutes,
        )
    elif args.window_minutes is not None:
        df = model.get_track_solar_window(
            track=args.track,
            end_time=args.timestamp,
            window_minutes=args.window_minutes,
        )
        result = {
            "track": args.track,
            "window_minutes": args.window_minutes,
            "track_avg_irradiance_wm2": float(df["avg_irradiance_wm2"].mean()),
            "track_avg_solar_power_w": float(df["avg_solar_power_w"].mean()),
            "best_point_id": int(df.sort_values("solar_score", ascending=False).iloc[0]["point_id"]),
            "weather_source": str(df["weather_source"].iloc[0]),
        }
    else:
        # Default to the first track point when no position is provided.
        point_id = args.point_id
        if point_id is None and args.distance_m is None and (args.lat is None or args.lon is None):
            point_id = 0

        result = model.get_solar_input(
            track=args.track,
            timestamp=args.timestamp,
            point_id=point_id,
            distance_m=args.distance_m,
            lat=args.lat,
            lon=args.lon,
            heading_deg=args.heading_deg,
            slope_deg=args.slope_deg,
        )

    print(json.dumps(result, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
