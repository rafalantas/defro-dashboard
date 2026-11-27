"""Retire superseded, regenerable candidate datasets from the shared SQLite store.

Why this exists: the curated Fibaro dataset and the heating aggregates are
versioned by a hash of their full content. Every time new data arrives the hash
changes, a new version is stored and *every* window is written again under it.
Without retention the table grows by a full copy of the history per version.

Scope is deliberately narrow and allow-listed. Only datasets that are derived
from raw evidence and can be rebuilt at any time are eligible, and only when:

* their status is ``candidate`` (anything promoted or otherwise labelled stays),
* they are not the newest ``keep`` versions of their name, and
* no model evaluation or model version refers to them.

Raw evidence (``fibaro_events``, ``fibaro_snapshots``, ``telemetry_samples``,
``refill_events``, ``weather_forecasts``), model artifacts, shadow predictions
and every dataset outside the allow-list are never touched.

The CLI defaults to a dry run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

SCHEMA = "thermoflow-dataset-retention/1"
DEFAULT_KEEP = 1

# dataset name -> feature sets whose rows carry that dataset's versions
ELIGIBLE_DATASETS = {
    "fibaro_heating_history_v2": ("fibaro_heating_event_v2",),
    "fibaro_heating_history_v3": ("fibaro_heating_event_v3",),
    "thermoflow_heating_aggregates": (
        "thermoflow_heating_hourly_v1", "thermoflow_heating_daily_v1",
    ),
}


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _is_referenced(db, tables, version):
    if "model_evaluations" in tables and db.execute(
        "SELECT 1 FROM model_evaluations WHERE dataset_version=? LIMIT 1", (version,)
    ).fetchone():
        return True
    if "model_versions" in tables and db.execute(
        "SELECT 1 FROM model_versions WHERE instr(metadata_json, ?) > 0 LIMIT 1", (version,)
    ).fetchone():
        return True
    return False


def retire_superseded_candidates(database_path=None, *, keep=DEFAULT_KEEP, dry_run=False):
    if type(keep) is not int or keep < 1:
        raise ValueError("keep must be a positive integer")
    path = Path(database_path or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")

    store = SQLiteStore(path)
    per_dataset, retired_total, windows_total = {}, 0, 0
    with store.connection() as db:
        tables = _tables(db)
        if not {"datasets", "feature_windows"}.issubset(tables):
            raise RuntimeError("ThermoFlow analytical schema is not ready")
        db.execute("BEGIN IMMEDIATE")
        for name, feature_sets in ELIGIBLE_DATASETS.items():
            rows = db.execute(
                "SELECT id,version,status FROM datasets WHERE name=? "
                "ORDER BY created_at DESC, id DESC", (name,),
            ).fetchall()
            kept = rows[:keep]
            kept_versions = {row["version"] for row in kept}
            retired, protected = [], []
            for row in rows[keep:]:
                if row["version"] in kept_versions:
                    continue
                if row["status"] != "candidate" or _is_referenced(db, tables, row["version"]):
                    protected.append(row["version"])
                    continue
                retired.append(row)
            placeholders = ",".join("?" for _ in feature_sets)
            windows = 0
            for row in retired:
                params = (*feature_sets, row["version"])
                clause = f"feature_set IN ({placeholders}) AND dataset_version=?"
                if dry_run:
                    windows += db.execute(
                        f"SELECT COUNT(*) FROM feature_windows WHERE {clause}", params
                    ).fetchone()[0]
                else:
                    windows += max(0, db.execute(
                        f"DELETE FROM feature_windows WHERE {clause}", params
                    ).rowcount)
                    db.execute("DELETE FROM datasets WHERE id=?", (row["id"],))
            per_dataset[name] = {
                "kept": [row["version"] for row in kept],
                "retired": [row["version"] for row in retired],
                "protected": protected,
                "feature_windows": windows,
            }
            retired_total += len(retired)
            windows_total += windows
    return {
        "schema": SCHEMA,
        "status": "dry_run" if dry_run else ("retired" if retired_total else "nothing_to_retire"),
        "dry_run": dry_run,
        "keep": keep,
        "datasets_retired": retired_total,
        "feature_windows_removed": windows_total,
        "per_dataset": per_dataset,
        "safety": {
            "raw_evidence_deleted": False,
            "model_artifacts_deleted": False,
            "allow_listed_datasets_only": sorted(ELIGIBLE_DATASETS),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", default=DEFAULT_DB_PATH)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help="newest versions to keep per dataset name (default 1)")
    parser.add_argument("--apply", action="store_true",
                        help="delete; without it nothing is changed (dry run)")
    args = parser.parse_args(argv)
    result = retire_superseded_candidates(args.database, keep=args.keep, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
