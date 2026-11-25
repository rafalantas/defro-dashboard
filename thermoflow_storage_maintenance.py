"""Explicit, backup-first cleanup of disposable ThermoFlow metadata.

The default operation is a dry-run. Only expired dashboard cache entries and
old collector-run diagnostics are eligible; source observations, refill events,
Fibaro history, features, datasets, model artifacts and evaluations are never
deleted by this utility.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from thermoflow_store import SQLiteStore

SCHEMA = "thermoflow-storage-maintenance/1"
DEFAULT_RUN_RETENTION_DAYS = 180


def plan_cleanup(database_path, *, now=None, run_retention_days=DEFAULT_RUN_RETENTION_DAYS):
    if type(run_retention_days) is not int or not 30 <= run_retention_days <= 3650:
        raise ValueError("run_retention_days must be between 30 and 3650")
    clock = int(time.time()) if now is None else now
    if type(clock) is not int or clock <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"ThermoFlow database does not exist: {path}")

    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cache_expired = 0
        runs_old = 0
        if "dashboard_cache" in tables:
            cache_expired = int(db.execute(
                "SELECT COUNT(*) FROM dashboard_cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (clock,),
            ).fetchone()[0])
        if "collection_runs" in tables:
            cutoff = clock - run_retention_days * 86400
            runs_old = int(db.execute(
                "SELECT COUNT(*) FROM collection_runs WHERE started_at < ?",
                (cutoff,),
            ).fetchone()[0])
        else:
            cutoff = clock - run_retention_days * 86400
    finally:
        db.close()
    return {
        "schema": SCHEMA,
        "database_file": path.name,
        "mode": "dry_run",
        "as_of": clock,
        "run_retention_days": run_retention_days,
        "collection_runs_cutoff": cutoff,
        "eligible": {
            "expired_dashboard_cache": cache_expired,
            "old_collection_runs": runs_old,
        },
        "eligible_total": cache_expired + runs_old,
        "protected_tables": [
            "telemetry_samples", "fibaro_events", "fibaro_snapshots",
            "refill_events", "weather_forecasts", "feature_windows",
            "datasets", "model_versions", "model_evaluations", "shadow_predictions",
            "collector_state",
        ],
        "writes_performed": False,
    }


def cleanup(database_path, *, now=None, run_retention_days=DEFAULT_RUN_RETENTION_DAYS,
            apply=False, backup_path=None):
    report = plan_cleanup(
        database_path, now=now, run_retention_days=run_retention_days
    )
    if not apply:
        return report
    if not backup_path:
        raise ValueError("--apply requires an explicit backup path")
    backup = Path(backup_path).expanduser()
    if backup.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {backup}")
    store = SQLiteStore(database_path)
    backup_result = store.backup_to(backup)
    if not backup_result.get("ok"):
        raise sqlite3.DatabaseError("backup did not pass integrity verification")

    clock = report["as_of"]
    cutoff = report["collection_runs_cutoff"]
    db = sqlite3.connect(str(Path(database_path).expanduser().resolve()), timeout=10)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cache_deleted = 0
        runs_deleted = 0
        if "dashboard_cache" in tables:
            cache_deleted = db.execute(
                "DELETE FROM dashboard_cache WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (clock,),
            ).rowcount
        if "collection_runs" in tables:
            runs_deleted = db.execute(
                "DELETE FROM collection_runs WHERE started_at < ?", (cutoff,)
            ).rowcount
        db.commit()
        check = [row[0] for row in db.execute("PRAGMA integrity_check")]
        if check != ["ok"]:
            raise sqlite3.DatabaseError(f"post-cleanup integrity check failed: {check}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        **report,
        "mode": "applied",
        "backup": str(backup),
        "backup_integrity_check": backup_result["integrity_check"],
        "deleted": {
            "expired_dashboard_cache": cache_deleted,
            "old_collection_runs": runs_deleted,
        },
        "writes_performed": True,
        "post_cleanup_integrity_check": ["ok"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--run-retention-days", type=int, default=DEFAULT_RUN_RETENTION_DAYS)
    parser.add_argument("--now", type=int, help="Unix timestamp override for reproducible runs")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup after creating a new verified backup")
    parser.add_argument("--backup", help="New backup file path; mandatory with --apply")
    args = parser.parse_args()
    result = cleanup(
        args.database, now=args.now, run_retention_days=args.run_retention_days,
        apply=args.apply, backup_path=args.backup,
    )
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
