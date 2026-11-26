"""Explicit one-command offline Burner evaluation and SQLite registration.

This command consumes an already prepared observed-cycle report. Training and
registration happen only when the existing untouched-holdout benchmark passes.
It never runs from the API scheduler or controls the boiler.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thermoflow_burner_artifact_store import persist_burner_shadow_result
from thermoflow_burner_live import build_live_artifact
from thermoflow_burner_ml import evaluate_burner_model
from thermoflow_store import SQLiteStore

SCHEMA = "thermoflow-burner-registration/1"


def register_burner_report(store, observed_report, *, recorded_at=None):
    """Evaluate an observed report and persist it only after all existing gates pass."""
    benchmark = evaluate_burner_model(observed_report)
    if not isinstance(benchmark, dict) or benchmark.get("status") != "evaluated":
        return {
            "schema": SCHEMA,
            "status": "skipped",
            "reason": benchmark.get("reason", "benchmark_not_evaluated")
            if isinstance(benchmark, dict) else "invalid_benchmark_result",
            "benchmark_status": benchmark.get("status")
            if isinstance(benchmark, dict) else None,
            "writes": 0,
            "promotion": "disabled",
        }

    artifact = build_live_artifact(benchmark, created_at=recorded_at)
    persisted = persist_burner_shadow_result(
        store, observed_report, benchmark, artifact, recorded_at=recorded_at
    )
    return {
        "schema": SCHEMA,
        "status": persisted.get("status"),
        "benchmark_status": benchmark.get("status"),
        "benchmark": {
            "candidate": (benchmark.get("evaluation") or {}).get("candidate"),
            "baselines": (benchmark.get("evaluation") or {}).get("baselines"),
        },
        "registration": persisted,
        "promotion": "manual_review_required",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="existing migrated ThermoFlow SQLite database")
    parser.add_argument("observed_report", type=Path)
    parser.add_argument("--recorded-at", type=int,
                        help="optional deterministic UTC timestamp for artifact records")
    args = parser.parse_args(argv)
    try:
        database = Path(args.database).expanduser()
        if not database.is_file():
            raise FileNotFoundError("ThermoFlow SQLite database does not exist")
        observed = json.loads(args.observed_report.read_text(encoding="utf-8"))
        result = register_burner_report(
            SQLiteStore(database), observed, recorded_at=args.recorded_at
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Burner registration failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("status") in {"persisted", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
