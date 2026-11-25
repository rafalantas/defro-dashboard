"""Read-only size and fragmentation diagnostics for the shared ThermoFlow SQLite DB.

This module never migrates, prunes, checkpoints, vacuums, or writes to the database.
It is intended to measure storage before any retention or compaction policy is chosen.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

SCHEMA = "thermoflow-storage-audit/1"

_PAYLOAD_COLUMNS = {
    "telemetry_samples": ("payload_json",),
    "fibaro_events": ("payload_json",),
    "fibaro_snapshots": ("payload_json",),
    "refill_events": ("payload_json",),
    "weather_forecasts": ("payload_json",),
    "feature_windows": ("values_json",),
    "datasets": ("manifest_json",),
    "model_versions": ("metadata_json",),
    "model_evaluations": ("metrics_json",),
    "shadow_predictions": ("prediction_json", "actual_json"),
    "dashboard_cache": ("payload_json",),
    "collector_state": ("metadata_json",),
    "collection_runs": ("details_json",),
}


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def inspect_storage(database_path: str | Path) -> dict:
    """Return row/payload/page/sidecar sizes using a read-only SQLite connection."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"ThermoFlow database does not exist: {path}")

    uri = path.as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_pages = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        present = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        tables = []
        for table in sorted(present.intersection(_PAYLOAD_COLUMNS)):
            quoted_table = '"' + table.replace('"', '""') + '"'
            count = int(db.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0])
            payload_bytes = 0
            for column in _PAYLOAD_COLUMNS[table]:
                quoted_column = '"' + column.replace('"', '""') + '"'
                payload_bytes += int(db.execute(
                    f"SELECT COALESCE(SUM(length({quoted_column})),0) FROM {quoted_table}"
                ).fetchone()[0])
            tables.append({
                "table": table,
                "rows": count,
                "payload_bytes": payload_bytes,
            })
    finally:
        db.close()

    tables.sort(key=lambda item: (-item["payload_bytes"], item["table"]))
    files = {
        "database": _file_size(path),
        "wal": _file_size(Path(str(path) + "-wal")),
        "shm": _file_size(Path(str(path) + "-shm")),
    }
    return {
        "schema": SCHEMA,
        "read_only": True,
        "database_file": path.name,
        "journal_mode": journal_mode,
        "page_size": page_size,
        "page_count": page_count,
        "allocated_bytes": page_size * page_count,
        "freelist_pages": freelist_pages,
        "reclaimable_bytes_estimate": page_size * freelist_pages,
        "files": files,
        "total_file_bytes": sum(files.values()),
        "tables": tables,
        "total_rows": sum(item["rows"] for item in tables),
        "total_payload_bytes": sum(item["payload_bytes"] for item in tables),
        "maintenance_performed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to an existing ThermoFlow SQLite database")
    args = parser.parse_args()
    print(json.dumps(inspect_storage(args.database), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
