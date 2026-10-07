from __future__ import annotations

import sys
from pathlib import Path

from influxdb_client import InfluxDBClient

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from settings import InfluxSettings


def main() -> None:
    settings = InfluxSettings.from_env()
    query = f'''
import "influxdata/influxdb/schema"

schema.measurementFieldKeys(
    bucket: "{settings.bucket}",
    measurement: "{settings.measurement}"
)
'''
    with InfluxDBClient(
        url=settings.url,
        token=settings.token,
        org=settings.org,
        timeout=settings.timeout_ms,
    ) as client:
        tables = client.query_api().query(query=query, org=settings.org)

    fields = sorted(
        {record.get_value() for table in tables for record in table.records}
    )
    print(f"\nFound {len(fields)} fields:\n")
    for field in fields:
        print(field)


if __name__ == "__main__":
    main()
