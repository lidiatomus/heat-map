from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class InfluxSettings:
    url: str
    token: str
    org: str
    bucket: str
    measurement: str
    ecu: str
    latitude_field: str
    longitude_field: str
    lookback: str
    timeout_ms: int

    @classmethod
    def from_env(
        cls,
        env_file: Optional[str | Path] = None,
        *,
        require_credentials: bool = True,
    ) -> "InfluxSettings":
        load_dotenv(dotenv_path=env_file, override=False)
        values = {
            "url": os.getenv("INFLUXDB_URL", "http://influxdb:8086"),
            "token": os.getenv("INFLUXDB_TOKEN", ""),
            "org": os.getenv("INFLUXDB_ORG", "solis"),
            "bucket": os.getenv("INFLUXDB_BUCKET", "telemetry"),
        }
        if require_credentials:
            missing = [
                env_name
                for key, env_name in (
                    ("token", "INFLUXDB_TOKEN"),
                    ("org", "INFLUXDB_ORG"),
                    ("bucket", "INFLUXDB_BUCKET"),
                )
                if not values[key]
            ]
            if missing:
                raise RuntimeError(
                    "Missing InfluxDB settings: " + ", ".join(missing)
                )

        return cls(
            url=values["url"].rstrip("/"),
            token=values["token"],
            org=values["org"],
            bucket=values["bucket"],
            measurement=os.getenv("INFLUXDB_MEASUREMENT", "solar_vehicle"),
            ecu=os.getenv("INFLUXDB_ECU", "VCU"),
            latitude_field=os.getenv(
                "INFLUXDB_LAT_FIELD", "VCU_GPS_Latitude"
            ),
            longitude_field=os.getenv(
                "INFLUXDB_LON_FIELD", "VCU_GPS_Longitude"
            ),
            lookback=os.getenv("INFLUXDB_LOOKBACK", "30s"),
            timeout_ms=int(os.getenv("INFLUXDB_TIMEOUT_MS", "15000")),
        )


@dataclass(frozen=True)
class WebSettings:
    default_track: str
    poll_seconds: float
    stale_after_seconds: float
    max_track_distance_km: float
    comparison_max_distance_km: float
    use_live_weather: bool

    @classmethod
    def from_env(cls) -> "WebSettings":
        load_dotenv(override=False)
        return cls(
            default_track=os.getenv("CSI_DEFAULT_TRACK", "targu_mures"),
            poll_seconds=float(os.getenv("CSI_POLL_SECONDS", "2")),
            stale_after_seconds=float(
                os.getenv("CSI_STALE_AFTER_SECONDS", "15")
            ),
            max_track_distance_km=float(
                os.getenv("CSI_MAX_TRACK_DISTANCE_KM", "10")
            ),
            comparison_max_distance_km=float(
                os.getenv("CSI_COMPARISON_MAX_DISTANCE_KM", "0.25")
            ),
            use_live_weather=os.getenv(
                "CSI_USE_LIVE_WEATHER", "true"
            ).strip().lower() in {"1", "true", "yes", "on"},
        )
