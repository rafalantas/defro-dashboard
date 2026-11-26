"""Scheduled local ThermoFlow analytics over the shared SQLite evidence.

The worker never contacts HC3, ThingSpeak or Open-Meteo. It refreshes the
curated Fibaro candidate dataset only when its content hash changes, persists
observed-only hourly/daily aggregates, and runs the existing offline shadow
pipeline. All model writes remain quality-gated and shadow-only.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path

from thermoflow_fibaro_curate import curate_database
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_fibaro_shadow_pipeline import (
    MAX_PIPELINE_SECONDS,
    run_fibaro_shadow_pipeline,
)
from thermoflow_heating_diagnostics import read_heating_analytics
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

DATASET_NAME = "fibaro_heating_history_v2"
DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
MIN_INTERVAL_SECONDS = 60 * 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60


def _source_window(store: SQLiteStore) -> dict:
    with store.connection() as db:
        row = db.execute(
            "SELECT COUNT(*) AS count, MIN(observed_at) AS first_at, "
            "MAX(observed_at) AS last_at FROM fibaro_events"
        ).fetchone()
        snapshots = int(db.execute(
            "SELECT COUNT(*) FROM fibaro_snapshots"
        ).fetchone()[0])
    return {
        "events": int(row["count"] or 0),
        "first_at": int(row["first_at"]) if row["first_at"] is not None else None,
        "last_at": int(row["last_at"]) if row["last_at"] is not None else None,
        "snapshots": snapshots,
    }


def _dataset_is_current(store: SQLiteStore, *, version: str, digest: str) -> bool:
    with store.connection() as db:
        row = db.execute(
            "SELECT content_sha256,status FROM datasets "
            "WHERE name=? AND version=?",
            (DATASET_NAME, version),
        ).fetchone()
    return bool(
        row is not None
        and row["content_sha256"] == digest
        and row["status"] == "candidate"
    )


def run_once(database_path: str = DEFAULT_DB_PATH, *, now: int | None = None) -> dict:
    """Refresh local analytical artifacts once; never mutates source evidence."""
    clock = int(time.time()) if now is None else now
    if type(clock) is not int or clock <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")

    store = SQLiteStore(path)
    source = _source_window(store)
    if source["events"] == 0:
        return {
            "schema": "thermoflow-analytics-scheduler/1",
            "status": "collecting",
            "reason": "no_fibaro_events",
            "source": source,
            "writes": False,
        }
    if source["snapshots"] == 0:
        return {
            "schema": "thermoflow-analytics-scheduler/1",
            "status": "blocked",
            "reason": "no_fibaro_snapshot_for_identity_validation",
            "source": source,
            "writes": False,
        }

    dry_run = curate_database(str(path), apply=False)
    if dry_run.get("curated_features") == 0:
        return {
            "schema": "thermoflow-analytics-scheduler/1",
            "status": "collecting",
            "reason": "no_validated_heating_transitions",
            "source": source,
            "curation": {"status": dry_run.get("status"), "curated_features": 0},
            "writes": False,
        }
    digest = dry_run.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return {
            "schema": "thermoflow-analytics-scheduler/1",
            "status": "blocked",
            "reason": "curation_did_not_produce_content_hash",
            "source": source,
            "curation": {"status": dry_run.get("status")},
            "writes": False,
        }

    dataset_version = "v2-" + digest[:16]
    if _dataset_is_current(store, version=dataset_version, digest=digest):
        curation = {"status": "unchanged", "dataset_version": dataset_version}
    else:
        # The existing curation command makes a verified SQLite backup before
        # writing and rechecks the raw-event count inside its transaction.
        curation = curate_database(str(path), apply=True)

    # Re-read the source bounds after curation; a concurrent collector insert
    # may have arrived after the dry-run. Its next cycle will curate it.
    source = _source_window(store)
    start = max(int(source["first_at"]), clock - MAX_PIPELINE_SECONDS)
    end = max(clock, int(source["last_at"]) + 1)
    if end <= start:
        return {
            "schema": "thermoflow-analytics-scheduler/1",
            "status": "collecting",
            "reason": "no_valid_analytics_window",
            "source": source,
            "curation": curation,
            "writes": bool(curation.get("status") == "applied"),
        }

    aggregates = read_heating_analytics(
        str(path), start=start, end=end, timezone_name="Europe/Warsaw",
        persist=True, recorded_at=clock,
    )
    collector = FibaroHistoryCollector(store)
    shadow = run_fibaro_shadow_pipeline(
        collector,
        start=start,
        end=end,
        timezone_name="Europe/Warsaw",
        minimum_events=50,
        minimum_observed_days=7,
        persist=True,
        recorded_at=clock,
    )
    return {
        "schema": "thermoflow-analytics-scheduler/1",
        "status": "completed",
        "window": {"start": start, "end": end},
        "source": source,
        "curation": curation,
        "aggregates": {
            "status": aggregates.get("status"),
            "persistence": aggregates.get("persistence"),
            "source_counts": aggregates.get("source_counts"),
        },
        "fibaro_shadow": {
            "status": shadow.get("status"),
            "reason": shadow.get("reason"),
            "evidence": shadow.get("evidence"),
            "quality": shadow.get("quality"),
            "readiness": shadow.get("readiness"),
            "persistence": shadow.get("persistence"),
        },
        "safety": {
            "hc3_requests": False,
            "external_network_requests": False,
            "raw_fibaro_events_deleted": False,
            "synthetic_observations_created": False,
            "device_commands_sent": False,
            "heating_settings_changed": False,
            "model_promotion": False,
            "mode": "offline_shadow_only",
        },
    }


def run_scheduler(cycle, *, interval_seconds=DEFAULT_INTERVAL_SECONDS,
                  max_cycles=None, wait=time.sleep):
    if (type(interval_seconds) is not int
            or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS):
        raise ValueError("interval_seconds must be between 3600 and 86400")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    results = []
    while max_cycles is None or len(results) < max_cycles:
        try:
            result = cycle()
            if max_cycles is not None:
                results.append(result)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        except Exception as error:
            # Keep logs useful without exposing paths, URLs or upstream details.
            failure = {
                "schema": "thermoflow-analytics-scheduler/1",
                "status": "error",
                "error_type": type(error).__name__,
            }
            if max_cycles is not None:
                results.append(failure)
            print(json.dumps(failure), flush=True)
        if max_cycles is None or len(results) < max_cycles:
            wait(interval_seconds)
    return results



def start_background_scheduler(
    database_path: str | None = None,
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    initial_delay_seconds: int | None = None,
    thread_factory=threading.Thread,
    wait=time.sleep,
):
    """Start one daemon worker inside the API process; return its thread."""
    if enabled is None:
        enabled = os.getenv("THERMOFLOW_ANALYTICS_ENABLED", "true").strip().lower() not in {
            "0", "false", "no", "off",
        }
    if not enabled:
        return None

    if interval_seconds is None:
        try:
            interval_seconds = int(os.getenv(
                "THERMOFLOW_ANALYTICS_INTERVAL_SECONDS",
                str(DEFAULT_INTERVAL_SECONDS),
            ))
        except (TypeError, ValueError):
            raise ValueError("THERMOFLOW_ANALYTICS_INTERVAL_SECONDS must be an integer") from None
    if (type(interval_seconds) is not int
            or not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS):
        raise ValueError("analytics interval must be between 3600 and 86400 seconds")

    if initial_delay_seconds is None:
        try:
            initial_delay_seconds = int(os.getenv(
                "THERMOFLOW_ANALYTICS_INITIAL_DELAY_SECONDS", "120"
            ))
        except (TypeError, ValueError):
            raise ValueError("THERMOFLOW_ANALYTICS_INITIAL_DELAY_SECONDS must be an integer") from None
    if type(initial_delay_seconds) is not int or not 0 <= initial_delay_seconds <= 3600:
        raise ValueError("analytics initial delay must be between 0 and 3600 seconds")

    path = database_path or os.getenv("THERMOFLOW_DB_PATH", DEFAULT_DB_PATH)

    def worker():
        if initial_delay_seconds:
            wait(initial_delay_seconds)
        run_scheduler(
            lambda: run_once(path),
            interval_seconds=interval_seconds,
        )

    thread = thread_factory(
        target=worker,
        name="thermoflow-analytics",
        daemon=True,
    )
    thread.start()
    return thread


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", default=DEFAULT_DB_PATH)
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not Path(args.database).expanduser().is_file():
        parser.error("database file does not exist; initialize the shared SQLite database first")
    if not MIN_INTERVAL_SECONDS <= args.interval_seconds <= MAX_INTERVAL_SECONDS:
        parser.error("--interval-seconds must be between 3600 and 86400")
    run_scheduler(
        lambda: run_once(args.database),
        interval_seconds=args.interval_seconds,
        max_cycles=1 if args.once else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
