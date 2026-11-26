"""Read-only access to validated ThermoFlow analytical data.

Raw Fibaro events remain an immutable source archive. This module reads only the
explicit v2 candidate dataset for analytics and reports source/derived storage
separately. It never migrates, prunes, rewrites, or promotes data.
"""
from __future__ import annotations

import json
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

DATASET_NAME = "fibaro_heating_history_v2"
FEATURE_SET = "fibaro_heating_event_v2"
FEATURE_SCHEMA = "thermoflow-fibaro-heating-feature/2"
MAX_WINDOW_SECONDS = 90 * 86400


def _window(start: int, end: int) -> None:
    if type(start) is not int or type(end) is not int or start <= 0 or end <= start:
        raise ValueError("window must contain positive ordered Unix timestamps")
    if end - start > MAX_WINDOW_SECONDS:
        raise ValueError("window cannot exceed 90 days")


def _readonly_store(database_path=None):
    path = Path(database_path or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")
    return SQLiteStore(path)


def read_analytics_inventory(database_path=None) -> dict:
    """Report raw archive and useful analytical storage as separate layers."""
    store = _readonly_store(database_path)
    with store.connection() as db:
        db.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        required = {"fibaro_events", "fibaro_snapshots", "feature_windows",
                    "datasets", "telemetry_samples", "collection_runs"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("ThermoFlow schema is incomplete: " + ", ".join(missing))

        def count(sql, params=()):
            return int(db.execute(sql, params).fetchone()[0])

        raw_events = count("SELECT COUNT(*) FROM fibaro_events")
        snapshots = count("SELECT COUNT(*) FROM fibaro_snapshots")
        telemetry = count("SELECT COUNT(*) FROM telemetry_samples")
        curated = count(
            "SELECT COUNT(*) FROM feature_windows WHERE feature_set=?",
            (FEATURE_SET,),
        )
        dataset_rows = db.execute(
            "SELECT version,status,content_sha256,manifest_json FROM datasets "
            "WHERE name=? ORDER BY created_at DESC,id DESC",
            (DATASET_NAME,),
        ).fetchall()
        feature_sets = [dict(row) for row in db.execute(
            "SELECT feature_set,COUNT(*) AS windows,MIN(window_start) AS first_at,"
            "MAX(window_end) AS last_at FROM feature_windows GROUP BY feature_set "
            "ORDER BY feature_set"
        )]
        telemetry_sources = [dict(row) for row in db.execute(
            "SELECT source,COUNT(*) AS samples,MIN(observed_at) AS first_at,"
            "MAX(observed_at) AS last_at FROM telemetry_samples GROUP BY source "
            "ORDER BY source"
        )]
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0])

    latest = None
    if dataset_rows:
        row = dataset_rows[0]
        try:
            manifest = json.loads(row["manifest_json"])
        except (TypeError, ValueError):
            manifest = None
        latest = {
            "version": row["version"], "status": row["status"],
            "content_sha256": row["content_sha256"], "manifest": manifest,
        }
    return {
        "schema": "thermoflow-analytics-inventory/1",
        "status": "available",
        "storage": {
            "raw_archive": {"fibaro_events": raw_events, "fibaro_snapshots": snapshots},
            "analytical": {"fibaro_v2_feature_windows": curated,
                           "latest_fibaro_dataset": latest,
                           "telemetry_samples": telemetry},
            "feature_sets": feature_sets,
            "telemetry_sources": telemetry_sources,
            "sqlite_pages": {"page_count": page_count, "freelist_count": freelist_count},
        },
        "policy": {
            "raw_source_events_deleted": False,
            "raw_events_are_analytics_input": False,
            "candidate_auto_promoted": False,
            "database_modified": False,
            "schema_migrations_run": False,
        },
        "read_only": True,
    }


def read_curated_fibaro_features(database_path=None, *, start: int, end: int,
                                  limit: int = 50000) -> dict:
    """Read only validated v2 candidate windows; never fall back to raw events."""
    _window(start, end)
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError("limit must be between 1 and 100000")
    store = _readonly_store(database_path)
    with store.connection() as db:
        db.execute("PRAGMA query_only=ON")
        dataset = db.execute(
            "SELECT version,status,content_sha256,manifest_json FROM datasets "
            "WHERE name=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (DATASET_NAME,),
        ).fetchone()
        if dataset is None:
            return {"schema": "thermoflow-curated-fibaro/1", "status": "collecting",
                    "reason": "candidate_dataset_not_found", "features": [],
                    "count": 0, "read_only": True}
        if dataset["status"] != "candidate":
            return {"schema": "thermoflow-curated-fibaro/1", "status": "blocked",
                    "reason": "dataset_status_not_candidate", "features": [],
                    "count": 0, "read_only": True}
        rows = db.execute(
            "SELECT window_start,window_end,dataset_version,values_json "
            "FROM feature_windows WHERE feature_set=? AND dataset_version=? "
            "AND window_start < ? AND window_end > ? "
            "ORDER BY window_start LIMIT ?",
            (FEATURE_SET, dataset["version"], end, start, limit),
        ).fetchall()

    features = []
    rejected = 0
    for row in rows:
        try:
            value = json.loads(row["values_json"])
        except (TypeError, ValueError):
            rejected += 1
            continue
        if (not isinstance(value, dict) or value.get("schema") != FEATURE_SCHEMA
                or value.get("source") != "fibaro-hc3"
                or type(value.get("observed_at")) is not int
                or not isinstance(value.get("transitions"), list)):
            rejected += 1
            continue
        features.append({
            "window_start": int(row["window_start"]),
            "window_end": int(row["window_end"]),
            "dataset_version": row["dataset_version"],
            "values": value,
        })
    return {
        "schema": "thermoflow-curated-fibaro/1",
        "status": "available" if features else "collecting",
        "dataset": {"name": DATASET_NAME, "version": dataset["version"],
                    "status": dataset["status"], "content_sha256": dataset["content_sha256"]},
        "window": {"start": start, "end": end},
        "features": features,
        "count": len(features),
        "rejected_malformed_windows": rejected,
        "limit": limit,
        "read_only": True,
        "raw_events_used": False,
    }
