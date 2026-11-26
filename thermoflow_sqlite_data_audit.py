"""Read-only SQLite inventory and ML data-readiness audit for ThermoFlow."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from thermoflow_model_monitoring import monitor_sqlite_database
from thermoflow_store import DEFAULT_DB_PATH

SCHEMA = "thermoflow-sqlite-data-audit/1"
MAX_DAYS = 90

TABLES = {
    "fibaro_events": ("observed_at",),
    "fibaro_snapshots": ("captured_at",),
    "telemetry_samples": ("observed_at",),
    "refill_events": ("occurred_at",),
    "weather_forecasts": ("fetched_at",),
    "feature_windows": ("window_start",),
    "datasets": ("created_at",),
    "model_versions": ("created_at",),
    "shadow_predictions": ("predicted_at",),
    "model_evaluations": ("evaluated_at", "created_at"),
    "collection_runs": ("started_at",),
}
REQUIRED_TABLES = {
    "fibaro_events", "fibaro_snapshots", "telemetry_samples",
    "refill_events", "weather_forecasts", "feature_windows",
    "datasets", "model_versions", "shadow_predictions", "collection_runs",
}


def _open_readonly(path: Path):
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _table_summary(db, table, *, start, end):
    columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
    count = int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    timestamp = next((name for name in TABLES[table] if name in columns), None)
    result = {"count": count, "timestamp_column": timestamp,
              "first_at": None, "last_at": None,
              "window_count": None, "observed_utc_days": None}
    if timestamp is not None:
        row = db.execute(
            f'SELECT MIN("{timestamp}"), MAX("{timestamp}"), '
            f'SUM(CASE WHEN "{timestamp}" >= ? AND "{timestamp}" <= ? THEN 1 ELSE 0 END), '
            f'COUNT(DISTINCT CASE WHEN "{timestamp}" >= ? AND "{timestamp}" <= ? '
            f'THEN date("{timestamp}",\'unixepoch\') END) FROM "{table}"',
            (start, end, start, end),
        ).fetchone()
        result.update(first_at=row[0], last_at=row[1],
                      window_count=int(row[2] or 0),
                      observed_utc_days=int(row[3] or 0))
    return result


def _grouped_rows(db, table, group_column):
    columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
    if group_column not in columns:
        return []
    rows = db.execute(
        f'SELECT "{group_column}" AS name, COUNT(*) AS count '
        f'FROM "{table}" GROUP BY "{group_column}" ORDER BY "{group_column}"'
    ).fetchall()
    return [{"name": str(row["name"] or "unknown"), "count": int(row["count"])}
            for row in rows]


def _source_inventory(db, table, source_column, timestamp_column, *, start, end):
    if (table, source_column, timestamp_column) == (
        "telemetry_samples", "source", "observed_at"
    ):
        query = """
            SELECT source AS name, COUNT(*) AS count,
                   MIN(observed_at) AS first_at, MAX(observed_at) AS last_at,
                   SUM(CASE WHEN observed_at >= ? AND observed_at <= ? THEN 1 ELSE 0 END) AS window_count,
                   COUNT(DISTINCT CASE WHEN observed_at >= ? AND observed_at <= ?
                       THEN date(observed_at, 'unixepoch') END) AS observed_utc_days
            FROM telemetry_samples
            GROUP BY source ORDER BY source
        """
    elif (table, source_column, timestamp_column) == (
        "refill_events", "source", "occurred_at"
    ):
        query = """
            SELECT source AS name, COUNT(*) AS count,
                   MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at,
                   SUM(CASE WHEN occurred_at >= ? AND occurred_at <= ? THEN 1 ELSE 0 END) AS window_count,
                   COUNT(DISTINCT CASE WHEN occurred_at >= ? AND occurred_at <= ?
                       THEN date(occurred_at, 'unixepoch') END) AS observed_utc_days
            FROM refill_events
            GROUP BY source ORDER BY source
        """
    else:
        return []
    rows = db.execute(query, (start, end, start, end)).fetchall()
    return [{
        "name": str(row["name"] or "unknown"),
        "count": int(row["count"]),
        "first_at": int(row["first_at"]) if row["first_at"] is not None else None,
        "last_at": int(row["last_at"]) if row["last_at"] is not None else None,
        "window_count": int(row["window_count"] or 0),
        "observed_utc_days": int(row["observed_utc_days"] or 0),
    } for row in rows]


def _verified_label_counts(db, tables):
    if "shadow_predictions" not in tables:
        return {"total": 0, "verified": 0, "pending": 0, "malformed": 0}
    rows = db.execute(
        "SELECT actual_json FROM shadow_predictions ORDER BY id DESC LIMIT 10000"
    ).fetchall()
    verified = pending = malformed = 0
    for row in rows:
        raw = row["actual_json"]
        if raw is None:
            pending += 1
            continue
        try:
            actual = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            malformed += 1
            continue
        value = actual.get("value") if isinstance(actual, dict) else None
        valid_value = type(value) in (int, float) and math.isfinite(value)
        if isinstance(actual, dict) and (
            actual.get("verified") is True or actual.get("complete_observed_cycle") is True
        ) and valid_value:
            verified += 1
        else:
            pending += 1
    return {"total": len(rows), "verified": verified,
            "pending": pending, "malformed": malformed,
            "inspection_limit": 10000}


def read_sqlite_data_audit(database_path=DEFAULT_DB_PATH, *, now, days=30):
    """Inspect persisted sources and registered ML evidence without writing."""
    if type(now) is not int or now <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError("days must be between 1 and 90")
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")
    start = max(1, now - days * 86400)

    db = _open_readonly(path)
    try:
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = sorted(REQUIRED_TABLES - tables)
        inventory = {
            name: _table_summary(db, name, start=start, end=now)
            for name in TABLES if name in tables
        }
        telemetry_sources = _source_inventory(
            db, "telemetry_samples", "source", "observed_at", start=start, end=now
        ) if "telemetry_samples" in tables else []
        refill_sources = _source_inventory(
            db, "refill_events", "source", "occurred_at", start=start, end=now
        ) if "refill_events" in tables else []
        dataset_registry = _grouped_rows(db, "datasets", "status") if "datasets" in tables else []
        model_registry = _grouped_rows(db, "model_versions", "model_name") if "model_versions" in tables else []
        collector_registry = _grouped_rows(db, "collection_runs", "collector_name") if "collection_runs" in tables else []
        labels = _verified_label_counts(db, tables)
        schema_version = None
        if "schema_migrations" in tables:
            schema_version = int(db.execute(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations"
            ).fetchone()[0])
        integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check").fetchall()]
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        db.close()

    sources = {
        "fibaro_events": inventory.get("fibaro_events", {}).get("count", 0),
        "fibaro_snapshots": inventory.get("fibaro_snapshots", {}).get("count", 0),
        "emodul_telemetry": sum(item["count"] for item in telemetry_sources if item["name"] == "emodul"),
        "thingspeak_telemetry": sum(
            item["count"] for item in telemetry_sources if item["name"].startswith("thingspeak:")
        ),
        "thingspeak_refill_events": sum(
            item["count"] for item in refill_sources if item["name"].startswith("thingspeak")
        ),
        "fuel_refill_events": inventory.get("refill_events", {}).get("count", 0),
        "weather_forecasts": inventory.get("weather_forecasts", {}).get("count", 0),
        "curated_feature_windows": inventory.get("feature_windows", {}).get("count", 0),
    }
    absent_sources = [name for name, count in sources.items() if count == 0]
    if missing or integrity != ["ok"]:
        status = "blocked"
    elif absent_sources:
        status = "collecting"
    else:
        status = "observing"

    try:
        monitoring = monitor_sqlite_database(str(path), now=now)
    except Exception:
        monitoring = {
            "schema": "thermoflow-model-monitoring/1",
            "status": "collecting",
            "promotion": "disabled",
            "modules": {},
            "error": "monitoring_unavailable",
        }

    # A count of source rows or verified shadow labels is not a training dataset.
    training = {
        "status": "collecting" if labels["verified"] == 0 else "manual_review_required",
        "verified_shadow_labels": labels["verified"],
        "pending_shadow_labels": labels["pending"],
        "malformed_shadow_labels": labels["malformed"],
        "training_eligibility": False,
        "reason": "inventory_is_not_a_validated_model_dataset",
    }
    return {
        "schema": SCHEMA,
        "status": status,
        "as_of": now,
        "window": {"days": days, "start": start, "end": now},
        "database": {
            "schema_version": schema_version,
            "integrity": {"ok": integrity == ["ok"], "results": integrity},
            "sqlite_pages": {"page_count": page_count, "freelist_count": freelist_count},
            "missing_tables": missing,
        },
        "sources": sources,
        "inventory": inventory,
        "telemetry_by_source": telemetry_sources,
        "refill_events_by_source": refill_sources,
        "dataset_registry_by_status": dataset_registry,
        "model_registry_by_name": model_registry,
        "collector_runs_by_name": collector_registry,
        "shadow_labels": labels,
        "training_readiness": training,
        "model_monitoring": monitoring,
        "safety": {
            "read_only": True,
            "database_modified": False,
            "schema_migrations_run": False,
            "external_requests": False,
            "model_training": False,
            "model_promotion": False,
            "hc3_requests": False,
            "device_commands_sent": False,
            "heating_settings_changed": False,
        },
        "limitations": [
            "Observed event-free intervals are not classified as collector outages.",
            "Raw source counts do not establish feature or target validity.",
            "Fibaro historical head-to-zone mapping is not inferred.",
            "Training eligibility remains closed until model-specific dataset validation.",
        ],
    }
