from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd
from influxdb_client import InfluxDBClient

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from settings import InfluxSettings


GPS_FIELDS = [
    "VCU_GPS_Latitude",
    "VCU_GPS_Longitude",
    "VCU_GPS_Speed",
    "VCU_GPS_Altitude",
    "VCU_GPS_Satellites",
    "VCU_GPS_FixQuality",
    "VCU_GPS_Active",
]


def _flux_or_filter(fields: Iterable[str]) -> str:
    return " or ".join(f'r._field == "{field}"' for field in fields)


def query_gps_history(
    start: str,
    stop: str,
    aggregate_every: str = "1s",
) -> pd.DataFrame:
    settings = InfluxSettings.from_env()

    field_filter = _flux_or_filter(GPS_FIELDS)

    query = f'''from(bucket: "{settings.bucket}")
  |> range(start: time(v: "{start}"), stop: time(v: "{stop}"))
  |> filter(fn: (r) => r._measurement == "{settings.measurement}")
  |> filter(fn: (r) => {field_filter})
  |> filter(fn: (r) => r.ecu == "{settings.ecu}")
  |> aggregateWindow(every: {aggregate_every}, fn: last, createEmpty: false)
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''

    with InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.org,
        timeout=60_000,
    ) as client:
        result = client.query_api().query_data_frame(query=query, org=settings.org)

    if isinstance(result, list):
        frames = [
            frame
            for frame in result
            if isinstance(frame, pd.DataFrame) and not frame.empty
        ]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
    else:
        df = result.copy()

    if df.empty:
        return df

    df = df.drop(
        columns=[
            c
            for c in [
                "result",
                "table",
                "_start",
                "_stop",
                "_measurement",
                "ecu",
            ]
            if c in df.columns
        ],
        errors="ignore",
    )

    df["_time"] = pd.to_datetime(df["_time"], utc=True, errors="coerce")
    df = df.dropna(subset=["_time"]).sort_values("_time").reset_index(drop=True)

    for field in GPS_FIELDS:
        if field in df.columns:
            df[field] = pd.to_numeric(df[field], errors="coerce")

    return df


def filter_valid_positions(
    df: pd.DataFrame,
    require_fix: bool = False,
) -> pd.DataFrame:
    lat_col = "VCU_GPS_Latitude"
    lon_col = "VCU_GPS_Longitude"

    if lat_col not in df.columns or lon_col not in df.columns:
        raise RuntimeError(
            "Latitude/longitude columns are missing. "
            "Check the exact InfluxDB field names."
        )

    valid = df[
        df[lat_col].notna()
        & df[lon_col].notna()
        & (df[lat_col] != 0)
        & (df[lon_col] != 0)
        & df[lat_col].between(-90, 90)
        & df[lon_col].between(-180, 180)
    ].copy()

    if require_fix:
        if "VCU_GPS_Active" in valid.columns:
            valid = valid[valid["VCU_GPS_Active"] == 1]
        if "VCU_GPS_FixQuality" in valid.columns:
            valid = valid[valid["VCU_GPS_FixQuality"] > 0]
        if "VCU_GPS_Satellites" in valid.columns:
            valid = valid[valid["VCU_GPS_Satellites"] > 0]

    return valid.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check historical GPS telemetry from InfluxDB."
    )
    parser.add_argument(
        "--start",
        default="2026-06-09T00:00:00Z",
        help="Inclusive UTC start time.",
    )
    parser.add_argument(
        "--stop",
        default="2026-06-13T00:00:00Z",
        help="Exclusive UTC stop time.",
    )
    parser.add_argument(
        "--aggregate-every",
        default="1s",
        help="Window used to align GPS fields before pivoting.",
    )
    parser.add_argument(
        "--require-fix",
        action="store_true",
        help="Require GPS active, fix quality > 0 and satellites > 0 when available.",
    )
    parser.add_argument(
        "--output",
        default="outputs/gps_2026-06-09_to_2026-06-12.csv",
        help="CSV path for valid GPS rows.",
    )
    args = parser.parse_args()

    raw = query_gps_history(
        start=args.start,
        stop=args.stop,
        aggregate_every=args.aggregate_every,
    )

    if raw.empty:
        print("No GPS telemetry found in the selected interval.")
        return

    valid = filter_valid_positions(raw, require_fix=args.require_fix)

    print(f"Rows returned after pivot: {len(raw)}")
    print(f"Valid non-zero GPS rows: {len(valid)}")

    if valid.empty:
        print(
            "GPS fields exist, but no valid non-zero coordinates were found "
            "for this period."
        )
        print("\nLast rows returned:")
        print(raw.tail(10).to_string(index=False))
        return

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    valid.to_csv(output_path, index=False)

    print("\nFirst valid GPS rows:")
    print(valid.head(5).to_string(index=False))

    print("\nLast valid GPS rows:")
    print(valid.tail(5).to_string(index=False))

    latest = valid.iloc[-1]
    print("\nLatest valid position in selected period:")
    print(f"time:      {latest['_time']}")
    print(f"latitude:  {latest['VCU_GPS_Latitude']}")
    print(f"longitude: {latest['VCU_GPS_Longitude']}")

    if "VCU_GPS_Speed" in latest.index:
        print(f"speed:     {latest['VCU_GPS_Speed']} km/h")
    if "VCU_GPS_Altitude" in latest.index:
        print(f"altitude:  {latest['VCU_GPS_Altitude']} m")
    if "VCU_GPS_Satellites" in latest.index:
        print(f"satellites:{latest['VCU_GPS_Satellites']}")
    if "VCU_GPS_FixQuality" in latest.index:
        print(f"fix:       {latest['VCU_GPS_FixQuality']}")
    if "VCU_GPS_Active" in latest.index:
        print(f"active:    {latest['VCU_GPS_Active']}")

    print(f"\nSaved valid GPS data to: {output_path}")


if __name__ == "__main__":
    main()
