"""Read-only Fibaro SQLite collection/readiness audit; no inferred sampling gaps."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_store import SQLiteStore

MAX_WINDOW_SECONDS = 90 * 86400


def inspect_readiness(database_path, *, start, end, now=None):
    if (type(start) is not int or type(end) is not int or start <= 0
            or end <= start or end - start > MAX_WINDOW_SECONDS):
        raise ValueError("readiness window must be ordered and at most 90 days")
    clock = int(time.time()) if now is None else now
    if type(clock) is not int or clock <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")
    store = SQLiteStore(path)
    collector = FibaroHistoryCollector(store)
    events = collector.read_events(start=start, end=end, limit=100000)
    snapshots = collector.read_snapshots(start=start, end=end, limit=10000)
    snapshots_in_window = [row for row in snapshots if start <= row["captured_at"] <= end]
    timestamps = [row["timestamp"] for row in events
                  if isinstance(row, dict) and type(row.get("timestamp")) is int]
    observed_days = sorted({
        datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
        for stamp in timestamps
    })
    with store.connection() as db:
        runs = [dict(row) for row in db.execute(
            """SELECT started_at,completed_at,window_start,window_end,fetched,
                      inserted,duplicates,invalid,status
               FROM collection_runs
               WHERE collector_name='fibaro_hc3_history'
                 AND window_end >= ? AND window_start <= ?
               ORDER BY started_at""",
            (start, end),
        )]
    latest_snapshot = collector.read_latest_snapshot()
    snapshot_observed_at = latest_snapshot.get("last_observed_at") if latest_snapshot else None
    snapshot_age = (max(0, clock - snapshot_observed_at)
                    if type(snapshot_observed_at) is int else None)
    successful_runs = sum(run["status"] == "complete" for run in runs)
    partial_runs = sum(run["status"] != "complete" for run in runs)
    status = "ready" if timestamps and latest_snapshot else "collecting"
    return {
        "schema": "thermoflow-fibaro-readiness/1",
        "status": status,
        "window": {"start": start, "end": end, "duration_seconds": end - start},
        "events": {
            "count": len(events),
            "first_timestamp": min(timestamps) if timestamps else None,
            "last_timestamp": max(timestamps) if timestamps else None,
            "observed_utc_days": observed_days,
            "no_events_observed": not timestamps,
        },
        "snapshots": {
            "transitions_in_window": len(snapshots_in_window),
            "latest_distinct_captured_at": latest_snapshot.get("captured_at") if latest_snapshot else None,
            "last_observed_at": snapshot_observed_at,
            "age_seconds": snapshot_age,
        },
        "collection_runs": {
            "count": len(runs),
            "successful": successful_runs,
            "partial_or_failed": partial_runs,
            "last_run": runs[-1] if runs else None,
        },
        "semantics": {
            "history_is_event_driven": True,
            "empty_event_interval_proves_outage": False,
            "missing_days_are_synthesized": False,
            "unchanged_snapshots_are_repeated_in_history": False,
        },
        "read_only": True,
        "automatic_quality_promotion": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--now", type=int)
    args = parser.parse_args(argv)
    result = inspect_readiness(args.database, start=args.start, end=args.end, now=args.now)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
