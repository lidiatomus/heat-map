from __future__ import annotations

import argparse
import os
import time

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from config import TRACKS
from influx_gps import InfluxGPSClient
from solar_model import SolarModel


def gps_distance_to_track_km(latitude: float, longitude: float, track_df) -> float:
    track_lat = track_df["lat"].to_numpy(dtype=float)
    track_lon = track_df["lon"].to_numpy(dtype=float)
    lat1 = np.radians(float(latitude))
    lat2 = np.radians(track_lat)
    dlat = lat2 - lat1
    dlon = np.radians(track_lon - float(longitude))
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    )
    distances = 6371.0 * 2.0 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return float(np.min(distances))


def build_point(track: str, telemetry, solar, now_utc: pd.Timestamp) -> Point:
    gps_time = pd.Timestamp(telemetry.gps.timestamp)
    mppt_time = pd.Timestamp(telemetry.mppt.timestamp)
    if gps_time.tzinfo is None:
        gps_time = gps_time.tz_localize("UTC")
    if mppt_time.tzinfo is None:
        mppt_time = mppt_time.tz_localize("UTC")

    point = (
        Point(os.getenv("GRAFANA_INFLUX_MEASUREMENT", "solar_model_live"))
        .tag("track", track)
        .tag("sector", solar["sector"])
        .tag("weather_source", solar["weather_source"])
        .field("gps_latitude", float(telemetry.gps.latitude))
        .field("gps_longitude", float(telemetry.gps.longitude))
        .field("track_latitude", float(solar["lat"]))
        .field("track_longitude", float(solar["lon"]))
        .field("point_id", int(solar["point_id"]))
        .field("distance_m", float(solar["distance_m"]))
        .field("irradiance_wm2", float(solar["irradiance_wm2"]))
        .field("calculated_solar_power_w", float(solar["solar_power_w"]))
        .field("active_mppt_count", int(telemetry.mppt.active_mppt_count))
        .field("shadow_factor", float(solar["shadow_factor"]))
        .field("orientation_factor", float(solar["orientation_factor"]))
        .field("pitch_factor", float(solar["pitch_factor"]))
        .field(
            "gps_age_seconds",
            float((now_utc - gps_time.tz_convert("UTC")).total_seconds()),
        )
        .field(
            "mppt_age_seconds",
            float((now_utc - mppt_time.tz_convert("UTC")).total_seconds()),
        )
        .time(now_utc.to_pydatetime(), WritePrecision.NS)
    )
    if telemetry.mppt.measured_power_w is not None:
        point.field(
            "measured_solar_power_w",
            float(telemetry.mppt.measured_power_w),
        )
    return point


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish the live solar model and vehicle telemetry for Grafana."
    )
    parser.add_argument("--track", choices=list(TRACKS), default="bucharest")
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--lookback", default=None, help="Influx lookback, e.g. 90d.")
    parser.add_argument("--max-age-seconds", type=float, default=30.0)
    parser.add_argument("--max-track-distance-km", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="Publish one sample and exit.")
    parser.add_argument("--no-weather-api", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    gps_client = InfluxGPSClient.from_env()
    if args.lookback:
        gps_client.lookback = args.lookback

    output_bucket = os.getenv("GRAFANA_INFLUX_BUCKET", gps_client.bucket)
    model = SolarModel(use_weather_api=not args.no_weather_api)
    track_df, _ = model.load_track(args.track)

    print(
        f"Publishing track={args.track} to bucket={output_bucket}, "
        f"measurement={os.getenv('GRAFANA_INFLUX_MEASUREMENT', 'solar_model_live')}"
    )

    with InfluxDBClient(
        url=gps_client.url,
        token=gps_client.token,
        org=gps_client.org,
        timeout=15000,
    ) as influx:
        write_api = influx.write_api(write_options=SYNCHRONOUS)

        while True:
            try:
                telemetry = gps_client.get_latest_telemetry()
                now_utc = pd.Timestamp.now(tz="UTC")
                gps_time = pd.Timestamp(telemetry.gps.timestamp)
                if gps_time.tzinfo is None:
                    gps_time = gps_time.tz_localize("UTC")
                age_seconds = (
                    now_utc - gps_time.tz_convert("UTC")
                ).total_seconds()
                if age_seconds > args.max_age_seconds:
                    raise RuntimeError(
                        f"GPS sample is stale: {age_seconds:.1f}s old "
                        f"(limit {args.max_age_seconds:.1f}s)."
                    )

                track_distance_km = gps_distance_to_track_km(
                    telemetry.gps.latitude,
                    telemetry.gps.longitude,
                    track_df,
                )
                if track_distance_km > args.max_track_distance_km:
                    raise RuntimeError(
                        f"GPS is {track_distance_km:.2f} km from "
                        f"the selected track '{args.track}'."
                    )

                solar = model.get_solar_input(
                    track=args.track,
                    timestamp=gps_time,
                    lat=telemetry.gps.latitude,
                    lon=telemetry.gps.longitude,
                )
                point = build_point(args.track, telemetry, solar, now_utc)
                write_api.write(
                    bucket=output_bucket,
                    org=gps_client.org,
                    record=point,
                )
                measured = telemetry.mppt.measured_power_w
                measured_text = "n/a" if measured is None else f"{measured:.1f} W"
                print(
                    f"[{now_utc.strftime('%H:%M:%S')}] "
                    f"GPS=({telemetry.gps.latitude:.6f}, "
                    f"{telemetry.gps.longitude:.6f}) | "
                    f"model={solar['solar_power_w']:.1f} W | "
                    f"MPPT={measured_text}"
                )
            except Exception as exc:
                print(f"[grafana-publisher] {exc}")

            if args.once:
                break
            time.sleep(max(args.interval_seconds, 0.2))


if __name__ == "__main__":
    main()
