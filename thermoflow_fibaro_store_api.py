"""Read-only API adapters for Fibaro evidence persisted in ThermoFlow SQLite."""
from __future__ import annotations

from pathlib import Path

from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_fibaro_shadow_pipeline import run_fibaro_shadow_pipeline
from thermoflow_store import SQLiteStore

MAX_READ_SECONDS = 90 * 86400


def _collector(database_path):
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database is not initialized")
    store = SQLiteStore(path)
    return store, FibaroHistoryCollector(store)


def _window(start, end):
    if (type(start) is not int or type(end) is not int or start <= 0
            or end <= start or end - start > MAX_READ_SECONDS):
        raise ValueError("Fibaro SQLite query window must be ordered and at most 90 days")


def read_fibaro_store_status(database_path):
    store, collector = _collector(database_path)
    return {
        "schema": "thermoflow-fibaro-store-api/1",
        "status": "available",
        "collector": collector.status(),
        "schema_version": store.schema_version(),
        "journal_mode": store.journal_mode(),
        "integrity_check": store.integrity_check(),
        "read_only": True,
    }


def read_fibaro_store_history(database_path, *, start, end, event_limit=100000,
                              snapshot_limit=10000):
    _window(start, end)
    _, collector = _collector(database_path)
    events = collector.read_events(start=start, end=end, limit=event_limit)
    snapshots = collector.read_snapshots(start=start, end=end, limit=snapshot_limit)
    return {
        "schema": "thermoflow-fibaro-store-history/1",
        "status": "available",
        "window": {"start": start, "end": end},
        "events": events,
        "snapshots": snapshots,
        "counts": {"events": len(events), "snapshots": len(snapshots)},
        "read_only": True,
    }


def read_fibaro_store_shadow(database_path, *, start, end,
                             timezone_name="Europe/Warsaw",
                             minimum_events=50, minimum_observed_days=7):
    _window(start, end)
    _, collector = _collector(database_path)
    result = run_fibaro_shadow_pipeline(
        collector, start=start, end=end, timezone_name=timezone_name,
        minimum_events=minimum_events,
        minimum_observed_days=minimum_observed_days,
        persist=False,
    )
    result["source"] = "shared_sqlite"
    return result
