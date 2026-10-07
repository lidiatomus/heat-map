from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import TRACKS
from influx_gps import InfluxGPSClient
from settings import WebSettings
from solar_model import SolarModel


WEB_DIR = Path(__file__).resolve().parent / "web"
app = FastAPI(title="CSI Live Track", version="1.0.0")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@lru_cache(maxsize=1)
def web_settings() -> WebSettings:
    settings = WebSettings.from_env()
    if settings.default_track not in TRACKS:
        raise RuntimeError(
            f"Unknown CSI_DEFAULT_TRACK '{settings.default_track}'. "
            f"Available: {list(TRACKS)}"
        )
    return settings


@lru_cache(maxsize=1)
def influx_client() -> InfluxGPSClient:
    return InfluxGPSClient.from_env()


@lru_cache(maxsize=1)
def solar_model() -> SolarModel:
    return SolarModel(use_weather_api=web_settings().use_live_weather)


@lru_cache(maxsize=len(TRACKS))
def track_dataframe(track: str) -> pd.DataFrame:
    if track not in TRACKS:
        raise HTTPException(status_code=404, detail=f"Unknown track: {track}")
    path = TRACKS[track]["track_3d_csv"]
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"Track data missing: {path.name}",
        )
    df = pd.read_csv(path, usecols=["point_id", "lat", "lon", "distance_m"])
    if df.empty:
        raise HTTPException(status_code=503, detail="Track data is empty")
    return df


def distance_to_track_km(latitude: float, longitude: float, df: pd.DataFrame) -> float:
    track_lat = np.radians(df["lat"].to_numpy(dtype=float))
    track_lon = np.radians(df["lon"].to_numpy(dtype=float))
    lat = np.radians(float(latitude))
    lon = np.radians(float(longitude))
    dlat = track_lat - lat
    dlon = track_lon - lon
    a = np.sin(dlat / 2) ** 2 + np.cos(lat) * np.cos(track_lat) * np.sin(dlon / 2) ** 2
    return float(6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1))).min())


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    settings = web_settings()
    track_dataframe(settings.default_track)
    return {"status": "ready", "default_track": settings.default_track}


@app.get("/api/config")
def api_config():
    settings = web_settings()
    return {
        "default_track": settings.default_track,
        "poll_seconds": settings.poll_seconds,
        "stale_after_seconds": settings.stale_after_seconds,
        "live_weather": settings.use_live_weather,
        "tracks": [
            {"key": key, "name": cfg["name"]}
            for key, cfg in TRACKS.items()
            if cfg["track_3d_csv"].exists()
        ],
    }


@app.get("/api/track/{track}")
def api_track(track: str):
    df = track_dataframe(track)
    stride = max(len(df) // 1600, 1)
    sampled = df.iloc[::stride]
    if sampled.index[-1] != df.index[-1]:
        sampled = pd.concat([sampled, df.tail(1)])
    return {
        "key": track,
        "name": TRACKS[track]["name"],
        "length_m": float(df["distance_m"].max()),
        "points": sampled[["lon", "lat"]].to_numpy(dtype=float).tolist(),
    }


def _finite_or_none(value):
    value = float(value)
    return value if np.isfinite(value) else None


@app.get("/api/dashboard/{track}")
def api_dashboard(track: str):
    track_df = track_dataframe(track)
    settings = web_settings()
    gps = None
    mppt = None
    try:
        gps = influx_client().get_latest_position()
    except Exception:
        pass
    try:
        mppt = influx_client().get_latest_mppt_power()
    except Exception:
        pass

    # Live weather must not inherit the timestamp of a stale GPS sample.
    # GPS controls only the car marker; solar/weather always use current time.
    timestamp = pd.Timestamp.now(tz="UTC")

    try:
        solar = solar_model().get_track_solar_input(track, timestamp)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Solar model unavailable: {exc}",
        ) from exc

    stride = max(len(solar) // 900, 1)
    sampled = solar.iloc[::stride]
    if sampled.index[-1] != solar.index[-1]:
        sampled = pd.concat([sampled, solar.tail(1)])

    racing_path = WEB_DIR.parent.parent / "outputs" / "plots" / f"{track}_ideal_racing_line_live.csv"
    racing_points = []
    if racing_path.exists():
        racing = pd.read_csv(racing_path, usecols=["lon", "lat"])
        racing_stride = max(len(racing) // 900, 1)
        racing_points = racing.iloc[::racing_stride][["lon", "lat"]].to_numpy(dtype=float).tolist()

    sectors = (
        solar.groupby("sector", observed=False)
        .agg(
            avg_irradiance_wm2=("irradiance_wm2", "mean"),
            avg_solar_power_w=("solar_power_w", "mean"),
        )
        .reset_index()
    )
    gps_usable = False
    gps_reason = "No GPS sample"
    if gps is not None:
        gps_timestamp = pd.Timestamp(gps.timestamp)
        if gps_timestamp.tzinfo is None:
            gps_timestamp = gps_timestamp.tz_localize("UTC")
        gps_age_seconds = max(
            0.0,
            float((timestamp - gps_timestamp.tz_convert("UTC")).total_seconds()),
        )
        coordinates_valid = (
            np.isfinite(gps.latitude)
            and np.isfinite(gps.longitude)
            and not (abs(gps.latitude) < 1e-9 and abs(gps.longitude) < 1e-9)
        )
        gps_distance_km = (
            distance_to_track_km(gps.latitude, gps.longitude, track_df)
            if coordinates_valid
            else None
        )
        gps_usable = bool(
            coordinates_valid
            and gps_age_seconds <= settings.stale_after_seconds
            and gps_distance_km is not None
            and gps_distance_km <= settings.comparison_max_distance_km
        )
        if not coordinates_valid:
            gps_reason = "Invalid GPS coordinates"
        elif gps_age_seconds > settings.stale_after_seconds:
            gps_reason = "GPS sample is stale"
        elif gps_distance_km is None or gps_distance_km > settings.comparison_max_distance_km:
            gps_reason = "GPS position is outside the selected circuit"
        else:
            gps_reason = "Live GPS matched to circuit"

    if gps_usable:
        nearest_index = (
            (track_df["lat"] - float(gps.latitude)) ** 2
            + (track_df["lon"] - float(gps.longitude)) ** 2
        ).idxmin()
        nearest_solar = solar.loc[nearest_index]
        calculated_solar_power_w = _finite_or_none(nearest_solar["solar_power_w"])
    else:
        calculated_solar_power_w = None

    return {
        "track": track,
        "timestamp": timestamp.isoformat(),
        "weather_source": str(solar["weather_source"].iloc[0]),
        "irradiance": [
            [float(row.lon), float(row.lat), _finite_or_none(row.irradiance_wm2)]
            for row in sampled.itertuples()
        ],
        "irradiance_min_wm2": _finite_or_none(solar["irradiance_wm2"].min()),
        "irradiance_max_wm2": _finite_or_none(solar["irradiance_wm2"].max()),
        "racing_line_available": bool(racing_points),
        "racing_line": racing_points,
        "calculated_solar_power_w": calculated_solar_power_w,
        "comparison_available": gps_usable,
        "comparison_status": gps_reason,
        "measured_mppt_power_w": (
            _finite_or_none(mppt.measured_power_w)
            if mppt is not None and mppt.measured_power_w is not None
            else None
        ),
        "active_mppt_count": int(mppt.active_mppt_count) if mppt is not None else 0,
        "sectors": [
            {
                "name": str(row.sector),
                "avg_irradiance_wm2": _finite_or_none(row.avg_irradiance_wm2),
                "avg_solar_power_w": _finite_or_none(row.avg_solar_power_w),
            }
            for row in sectors.itertuples()
        ],
    }


@app.get("/api/live/{track}")
def api_live(track: str):
    df = track_dataframe(track)
    settings = web_settings()
    try:
        gps = influx_client().get_latest_position()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"InfluxDB telemetry unavailable: {exc}",
        ) from exc

    timestamp = pd.Timestamp(gps.timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    now = pd.Timestamp.now(tz="UTC")
    age_seconds = max(
        0.0,
        float((now - timestamp.tz_convert("UTC")).total_seconds()),
    )
    valid = (
        np.isfinite(gps.latitude)
        and np.isfinite(gps.longitude)
        and not (abs(gps.latitude) < 1e-9 and abs(gps.longitude) < 1e-9)
    )
    distance_km = (
        distance_to_track_km(gps.latitude, gps.longitude, df)
        if valid
        else None
    )
    on_track = bool(
        valid
        and distance_km is not None
        and distance_km <= settings.max_track_distance_km
    )
    stale = age_seconds > settings.stale_after_seconds

    return {
        "track": track,
        "latitude": float(gps.latitude),
        "longitude": float(gps.longitude),
        "timestamp": timestamp.isoformat(),
        "age_seconds": age_seconds,
        "stale": stale,
        "valid": bool(valid),
        "on_track": on_track,
        "distance_to_track_km": distance_km,
    }


@app.get("/api/history/{track}")
def api_history(track: str, start: datetime, stop: datetime):
    df = track_dataframe(track)
    settings = web_settings()
    if start.tzinfo is None or stop.tzinfo is None:
        raise HTTPException(status_code=400, detail="Start and stop must include a timezone")
    start = start.astimezone(timezone.utc)
    stop = stop.astimezone(timezone.utc)
    if stop <= start:
        raise HTTPException(status_code=400, detail="Stop must be after start")
    if (stop - start).total_seconds() > 31 * 24 * 3600:
        raise HTTPException(status_code=400, detail="Maximum history interval is 31 days")

    try:
        positions = influx_client().get_position_history(start, stop)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"InfluxDB history unavailable: {exc}",
        ) from exc

    margin = settings.comparison_max_distance_km / 80.0
    min_lat, max_lat = float(df["lat"].min()) - margin, float(df["lat"].max()) + margin
    min_lon, max_lon = float(df["lon"].min()) - margin, float(df["lon"].max()) + margin
    samples = []
    for position in positions:
        inside_bounds = (
            min_lat <= position.latitude <= max_lat
            and min_lon <= position.longitude <= max_lon
        )
        if not inside_bounds:
            continue
        if distance_to_track_km(position.latitude, position.longitude, df) > settings.comparison_max_distance_km:
            continue
        samples.append({
            "latitude": float(position.latitude),
            "longitude": float(position.longitude),
            "timestamp": pd.Timestamp(position.timestamp).isoformat(),
        })
    return {
        "track": track,
        "start": start.isoformat(),
        "stop": stop.isoformat(),
        "count": len(samples),
        "points": samples,
    }


@app.get("/api/history-frame/{track}")
def api_history_frame(
    track: str,
    timestamp: datetime,
    latitude: float,
    longitude: float,
    include_map: bool = False,
):
    track_df = track_dataframe(track)
    settings = web_settings()
    if timestamp.tzinfo is None:
        raise HTTPException(status_code=400, detail="Timestamp must include a timezone")
    distance_km = distance_to_track_km(latitude, longitude, track_df)
    if distance_km > settings.comparison_max_distance_km:
        raise HTTPException(
            status_code=400,
            detail=f"Historical GPS point is {distance_km:.2f} km from the selected circuit",
        )

    nearest_index = (
        (track_df["lat"] - float(latitude)) ** 2
        + (track_df["lon"] - float(longitude)) ** 2
    ).idxmin()
    try:
        if include_map:
            solar = solar_model().get_track_solar_input(track, timestamp)
            nearest_solar = solar.loc[nearest_index]
        else:
            point_id = int(track_df.loc[nearest_index, "point_id"])
            point = solar_model().get_solar_input(
                track=track,
                timestamp=timestamp,
                point_id=point_id,
            )
            solar = None
            nearest_solar = point
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Historical solar model unavailable: {exc}") from exc

    try:
        mppt = influx_client().get_mppt_power_at(timestamp)
    except Exception:
        mppt = None

    if isinstance(nearest_solar, dict):
        calculated_power = _finite_or_none(nearest_solar["solar_power_w"])
        point_irradiance = _finite_or_none(nearest_solar["irradiance_wm2"])
        weather_source = str(nearest_solar["weather_source"])
    else:
        calculated_power = _finite_or_none(nearest_solar["solar_power_w"])
        point_irradiance = _finite_or_none(nearest_solar["irradiance_wm2"])
        weather_source = str(nearest_solar["weather_source"])

    result = {
        "track": track,
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
        "weather_source": weather_source,
        "distance_to_track_km": distance_km,
        "point_irradiance_wm2": point_irradiance,
        "calculated_solar_power_w": calculated_power,
        "measured_mppt_power_w": (
            _finite_or_none(mppt.measured_power_w)
            if mppt is not None and mppt.measured_power_w is not None
            else None
        ),
        "active_mppt_count": int(mppt.active_mppt_count) if mppt is not None else 0,
    }
    if solar is not None:
        stride = max(len(solar) // 900, 1)
        sampled = solar.iloc[::stride]
        if sampled.index[-1] != solar.index[-1]:
            sampled = pd.concat([sampled, solar.tail(1)])
        result.update({
            "irradiance": [
                [float(row.lon), float(row.lat), _finite_or_none(row.irradiance_wm2)]
                for row in sampled.itertuples()
            ],
            "irradiance_min_wm2": _finite_or_none(solar["irradiance_wm2"].min()),
            "irradiance_max_wm2": _finite_or_none(solar["irradiance_wm2"].max()),
        })
    return result
