from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from settings import InfluxSettings


POWER_FIELDS = [
    f"MPPT{i}_{name}"
    for i in range(1, 5)
    for name in ("InputVoltage", "InputCurrent")
]

STATUS_FIELDS = [
    f"MPPT{i}_{name}"
    for i in range(1, 5)
    for name in ("Enabled", "Mode", "Fault")
]

ALL_FIELDS = POWER_FIELDS + STATUS_FIELDS


def query_history(start: str, stop: str) -> pd.DataFrame:
    settings = InfluxSettings.from_env()

    field_filter = " or ".join(
        f'r._field == "{field}"' for field in ALL_FIELDS
    )

    # Build the Flux query without putting Flux record braces inside a Python f-string.
    query = (
        f'from(bucket: "{settings.bucket}")\n'
        f'  |> range(start: time(v: "{start}"), stop: time(v: "{stop}"))\n'
        f'  |> filter(fn: (r) => r._measurement == "{settings.measurement}")\n'
        f'  |> filter(fn: (r) => {field_filter})\n'
        '  |> aggregateWindow(every: 1s, fn: last, createEmpty: false)\n'
        '  |> map(fn: (r) => ({r with _value: float(v: r._value)}))\n'
        '  |> drop(columns: ["ecu"])\n'
        '  |> group(columns: [])\n'
        '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> sort(columns: ["_time"])\n'
    )

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

    df["_time"] = pd.to_datetime(df["_time"], utc=True, errors="coerce")
    df = df.dropna(subset=["_time"]).sort_values("_time")

    drop_cols = ["result", "table", "_start", "_stop", "_measurement"]
    df = df.drop(
        columns=[c for c in drop_cols if c in df.columns],
        errors="ignore",
    )

    for field in ALL_FIELDS:
        if field in df.columns:
            df[field] = pd.to_numeric(df[field], errors="coerce")

    df = df.set_index("_time").resample("1s").last()

    present_status = [c for c in STATUS_FIELDS if c in df.columns]
    if present_status:
        df[present_status] = df[present_status].ffill(limit=10)

    return df.reset_index()


def calculate_valid_power(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    valid_power_cols = []
    active_cols = []

    for i in range(1, 5):
        voltage_col = f"MPPT{i}_InputVoltage"
        current_col = f"MPPT{i}_InputCurrent"
        enabled_col = f"MPPT{i}_Enabled"
        mode_col = f"MPPT{i}_Mode"
        fault_col = f"MPPT{i}_Fault"

        power_col = f"MPPT{i}_MeasuredPower_W"
        active_col = f"MPPT{i}_ActiveValid"

        for col in (
            voltage_col,
            current_col,
            enabled_col,
            mode_col,
            fault_col,
        ):
            if col not in out.columns:
                out[col] = np.nan

        status_valid = (
            (out[enabled_col] == 1)
            & (out[fault_col] == 0)
            & out[mode_col].between(0, 5)
        )

        electrical_valid = (
            out[voltage_col].between(0, 200)
            & out[current_col].abs().between(0, 16)
        )

        raw_power = -(out[voltage_col] * out[current_col])

        out[active_col] = (
            status_valid
            & electrical_valid
            & (raw_power >= 0)
        )
        out[power_col] = raw_power.where(out[active_col])

        active_cols.append(active_col)
        valid_power_cols.append(power_col)

    out["active_mppt_count"] = out[active_cols].sum(axis=1)
    out["measured_solar_power_w"] = out[valid_power_cols].sum(
        axis=1,
        min_count=1,
    )

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-06-09T00:00:00Z")
    parser.add_argument("--stop", default="2026-06-13T00:00:00Z")
    parser.add_argument(
        "--output",
        default="outputs/mppt_with_status_2026-06-09_to_2026-06-12.csv",
    )
    args = parser.parse_args()

    df = query_history(args.start, args.stop)
    if df.empty:
        print("No MPPT power/status data found.")
        return

    out = calculate_valid_power(df)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output, index=False)

    print(f"Total one-second rows: {len(out)}")
    print(
        "Rows with at least one active valid MPPT: "
        f"{(out['active_mppt_count'] > 0).sum()}"
    )
    print(
        "Rows with all four active valid MPPTs: "
        f"{(out['active_mppt_count'] == 4).sum()}"
    )

    for i in range(1, 5):
        enabled = f"MPPT{i}_Enabled"
        fault = f"MPPT{i}_Fault"
        mode = f"MPPT{i}_Mode"

        print(
            f"MPPT{i}: enabled=1 rows "
            f"{int((out[enabled] == 1).sum())}, "
            f"fault=0 rows "
            f"{int((out[fault] == 0).sum())}, "
            f"normal mode rows "
            f"{int(out[mode].between(0, 5).sum())}"
        )

    valid = out[out["active_mppt_count"] > 0]

    if not valid.empty:
        print("\nMeasured power for status-valid rows:")
        print(valid["measured_solar_power_w"].describe().to_string())

        print("\nFirst valid rows:")
        cols = [
            "_time",
            "active_mppt_count",
            "measured_solar_power_w",
        ]
        print(valid[cols].head(10).to_string(index=False))
    else:
        print(
            "\nNo rows have an MPPT simultaneously enabled, "
            "fault-free and in a normal mode."
        )

    print(f"\nSaved: {output}")


if __name__ == "__main__":
    main()
