"""Standalone live solar dashboard with racing line, without Influx telemetry."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_dashboard_module():
    dashboard_path = Path(__file__).with_name("08_solar_dashboard_influx.py")
    spec = importlib.util.spec_from_file_location(
        "solar_dashboard_influx",
        dashboard_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load dashboard module: {dashboard_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    dashboard = _load_dashboard_module()
    args = dashboard.parse_args()
    if args.live_influx or args.comparison_csv:
        raise ValueError(
            "This standalone dashboard does not use Influx or replay CSV. "
            "Run 08_solar_dashboard_influx.py for telemetry modes."
        )
    dashboard.run_dashboard(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
