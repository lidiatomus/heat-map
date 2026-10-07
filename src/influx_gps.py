from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from influxdb_client import InfluxDBClient

from settings import InfluxSettings


@dataclass(frozen=True)
class GPSPosition:
    latitude: float
    longitude: float
    timestamp: pd.Timestamp
    measurement: str
    ecu: str


@dataclass(frozen=True)
class MPPTPower:
    measured_power_w: Optional[float]
    active_mppt_count: int
    timestamp: pd.Timestamp


@dataclass(frozen=True)
class LiveTelemetry:
    gps: GPSPosition
    mppt: MPPTPower


class InfluxGPSClient:
    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        measurement: str = "solar_vehicle",
        ecu: str = "VCU",
        latitude_field: str = "VCU_GPS_Latitude",
        longitude_field: str = "VCU_GPS_Longitude",
        lookback: str = "30d",
        timeout_ms: int = 15000,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.org = org
        self.bucket = bucket
        self.measurement = measurement
        self.ecu = ecu
        self.latitude_field = latitude_field
        self.longitude_field = longitude_field
        self.lookback = lookback
        self.timeout_ms = timeout_ms

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "InfluxGPSClient":
        settings = InfluxSettings.from_env(env_file)
        return cls(
            url=settings.url,
            token=settings.token,
            org=settings.org,
            bucket=settings.bucket,
            measurement=settings.measurement,
            ecu=settings.ecu,
            latitude_field=settings.latitude_field,
            longitude_field=settings.longitude_field,
            lookback=settings.lookback,
            timeout_ms=settings.timeout_ms,
        )

    @staticmethod
    def _normalize_coordinate(value: float, is_latitude: bool) -> float:
        value = float(value)
        limit = 90.0 if is_latitude else 180.0

        if abs(value) <= limit:
            return value

        for scale in (1e7, 1e6, 1e5):
            candidate = value / scale
            if abs(candidate) <= limit:
                return candidate

        kind = "latitude" if is_latitude else "longitude"
        raise ValueError(f"Invalid {kind} value from InfluxDB: {value}")

    def _query_latest_field(self, field: str):
        query = f"""
from(bucket: "{self.bucket}")
  |> range(start: -{self.lookback})
  |> filter(fn: (r) => r._measurement == "{self.measurement}")
  |> filter(fn: (r) => r._field == "{field}")
  |> filter(fn: (r) => r.ecu == "{self.ecu}")
  |> last()
"""
        with InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=15000,
        ) as client:
            tables = client.query_api().query(query=query, org=self.org)

        records = [record for table in tables for record in table.records]
        if not records:
            raise RuntimeError(
                f"No InfluxDB value found for field '{field}' in the last {self.lookback}."
            )

        record = max(records, key=lambda item: item.get_time())
        return float(record.get_value()), pd.Timestamp(record.get_time())

    def get_latest_position(self) -> GPSPosition:
        lat_raw, lat_time = self._query_latest_field(self.latitude_field)
        lon_raw, lon_time = self._query_latest_field(self.longitude_field)

        return GPSPosition(
            latitude=self._normalize_coordinate(lat_raw, True),
            longitude=self._normalize_coordinate(lon_raw, False),
            # The pair is only as fresh as its older coordinate.
            timestamp=min(lat_time, lon_time),
            measurement=self.measurement,
            ecu=self.ecu,
        )

    def get_position_history(
        self,
        start: datetime,
        stop: datetime,
        max_points: int = 5000,
    ) -> list[GPSPosition]:
        duration_seconds = max((stop - start).total_seconds(), 1.0)
        window_seconds = max(1, int(duration_seconds / max_points) + 1)
        start_iso = pd.Timestamp(start).isoformat()
        stop_iso = pd.Timestamp(stop).isoformat()
        query = f'''\
from(bucket: "{self.bucket}")
  |> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}"))
  |> filter(fn: (r) => r._measurement == "{self.measurement}")
  |> filter(fn: (r) => r.ecu == "{self.ecu}")
  |> filter(fn: (r) => r._field == "{self.latitude_field}" or r._field == "{self.longitude_field}")
  |> aggregateWindow(every: {window_seconds}s, fn: last, createEmpty: false)
  |> keep(columns: ["_time", "_field", "_value"])
  |> sort(columns: ["_time"])
'''
        with InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=self.timeout_ms,
        ) as client:
            tables = client.query_api().query(query=query, org=self.org)

        by_field = {
            self.latitude_field: [],
            self.longitude_field: [],
        }
        for table in tables:
            for record in table.records:
                field = record.get_field()
                if field in by_field:
                    by_field[field].append(
                        (pd.Timestamp(record.get_time()), float(record.get_value()))
                    )

        latitude = pd.DataFrame(
            by_field[self.latitude_field], columns=["timestamp", "latitude"]
        ).sort_values("timestamp")
        longitude = pd.DataFrame(
            by_field[self.longitude_field], columns=["timestamp", "longitude"]
        ).sort_values("timestamp")
        if latitude.empty or longitude.empty:
            return []

        paired = pd.merge_asof(
            latitude,
            longitude,
            on="timestamp",
            direction="nearest",
            tolerance=pd.Timedelta(seconds=max(window_seconds * 2, 2)),
        ).dropna(subset=["latitude", "longitude"])
        positions = [
            GPSPosition(
                latitude=self._normalize_coordinate(row.latitude, True),
                longitude=self._normalize_coordinate(row.longitude, False),
                timestamp=pd.Timestamp(row.timestamp),
                measurement=self.measurement,
                ecu=self.ecu,
            )
            for row in paired.head(max_points).itertuples()
        ]
        return positions

    def get_latest_mppt_power(self) -> MPPTPower:
        fields = [
            f"MPPT{i}_{name}"
            for i in range(1, 5)
            for name in ("InputVoltage", "InputCurrent", "Enabled", "Mode", "Fault")
        ]
        field_filter = " or ".join(
            f'r._field == "{field}"' for field in fields
        )
        query = f"""
from(bucket: "{self.bucket}")
  |> range(start: -{self.lookback})
  |> filter(fn: (r) => r._measurement == "{self.measurement}")
  |> filter(fn: (r) => {field_filter})
  |> group(columns: ["_field"])
  |> last()
"""
        with InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=15000,
        ) as client:
            tables = client.query_api().query(query=query, org=self.org)

        records = [record for table in tables for record in table.records]
        if not records:
            raise RuntimeError(
                f"No MPPT values found in InfluxDB in the last {self.lookback}."
            )

        latest_by_field = {}
        for record in records:
            field = record.get_field()
            previous = latest_by_field.get(field)
            if previous is None or record.get_time() > previous.get_time():
                latest_by_field[field] = record

        active_count = 0
        valid_powers = []
        valid_times = []

        for i in range(1, 5):
            names = {
                name: f"MPPT{i}_{name}"
                for name in ("InputVoltage", "InputCurrent", "Enabled", "Mode", "Fault")
            }
            if not all(field in latest_by_field for field in names.values()):
                continue

            values = {
                name: float(latest_by_field[field].get_value())
                for name, field in names.items()
            }
            status_valid = (
                values["Enabled"] == 1
                and values["Fault"] == 0
                and 0 <= values["Mode"] <= 5
            )
            electrical_valid = (
                0 <= values["InputVoltage"] <= 200
                and 0 <= abs(values["InputCurrent"]) <= 16
            )
            power_w = -(values["InputVoltage"] * values["InputCurrent"])

            if status_valid and electrical_valid and power_w >= 0:
                active_count += 1
                valid_powers.append(power_w)
                valid_times.extend(
                    latest_by_field[field].get_time()
                    for field in names.values()
                )

        all_times = [record.get_time() for record in records]
        # A computed value is only as fresh as its oldest input field.
        timestamp = pd.Timestamp(
            min(valid_times)
            if valid_times
            else max(all_times)
        )
        measured_power = (
            float(sum(valid_powers))
            if valid_powers
            else None
        )
        return MPPTPower(
            measured_power_w=measured_power,
            active_mppt_count=active_count,
            timestamp=timestamp,
        )

    def get_mppt_power_at(
        self,
        timestamp: datetime,
        tolerance_seconds: int = 30,
    ) -> MPPTPower:
        target = pd.Timestamp(timestamp)
        if target.tzinfo is None:
            target = target.tz_localize("UTC")
        else:
            target = target.tz_convert("UTC")
        start = (target - timedelta(seconds=tolerance_seconds)).isoformat()
        stop = (target + timedelta(seconds=tolerance_seconds)).isoformat()
        fields = [
            f"MPPT{i}_{name}"
            for i in range(1, 5)
            for name in ("InputVoltage", "InputCurrent", "Enabled", "Mode", "Fault")
        ]
        field_filter = " or ".join(f'r._field == "{field}"' for field in fields)
        query = f'''\
from(bucket: "{self.bucket}")
  |> range(start: time(v: "{start}"), stop: time(v: "{stop}"))
  |> filter(fn: (r) => r._measurement == "{self.measurement}")
  |> filter(fn: (r) => {field_filter})
  |> keep(columns: ["_time", "_field", "_value"])
'''
        with InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=self.timeout_ms,
        ) as client:
            tables = client.query_api().query(query=query, org=self.org)

        records = [record for table in tables for record in table.records]
        if not records:
            return MPPTPower(None, 0, target)

        nearest_by_field = {}
        for record in records:
            field = record.get_field()
            distance = abs((pd.Timestamp(record.get_time()) - target).total_seconds())
            previous = nearest_by_field.get(field)
            if previous is None or distance < previous[0]:
                nearest_by_field[field] = (distance, record)

        active_count = 0
        powers = []
        used_times = []
        for i in range(1, 5):
            names = {
                name: f"MPPT{i}_{name}"
                for name in ("InputVoltage", "InputCurrent", "Enabled", "Mode", "Fault")
            }
            if not all(field in nearest_by_field for field in names.values()):
                continue
            selected = {
                name: nearest_by_field[field][1]
                for name, field in names.items()
            }
            values = {name: float(record.get_value()) for name, record in selected.items()}
            valid = (
                values["Enabled"] == 1
                and values["Fault"] == 0
                and 0 <= values["Mode"] <= 5
                and 0 <= values["InputVoltage"] <= 200
                and 0 <= abs(values["InputCurrent"]) <= 16
            )
            power = -(values["InputVoltage"] * values["InputCurrent"])
            if valid and power >= 0:
                active_count += 1
                powers.append(power)
                used_times.extend(record.get_time() for record in selected.values())

        return MPPTPower(
            measured_power_w=float(sum(powers)) if powers else None,
            active_mppt_count=active_count,
            timestamp=pd.Timestamp(min(used_times)) if used_times else target,
        )

    def get_latest_telemetry(self) -> LiveTelemetry:
        return LiveTelemetry(
            gps=self.get_latest_position(),
            mppt=self.get_latest_mppt_power(),
        )


if __name__ == "__main__":
    gps = InfluxGPSClient.from_env().get_latest_position()
    print(gps)
