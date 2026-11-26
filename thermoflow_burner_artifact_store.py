"""Persist evaluated Burner ML datasets, artifacts and holdout predictions in SQLite.

This is an explicit offline import. It performs no network access, migration,
training, model promotion, or controller interaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

from thermoflow_burner_live import load_live_artifact
from thermoflow_ml_burner_adapter import FEATURE_KEYS, TARGET_KEY
from thermoflow_ml_training import predict
from thermoflow_observed_cycles import REPORT_SCHEMA
from thermoflow_burner_ml import BENCHMARK_SCHEMA
from thermoflow_store import SQLiteStore

SCHEMA = "thermoflow-burner-artifact-store/1"
MODEL_NAME = "burner_cycle_duration"
FEATURE_SET = "burner_cycle_v1"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_schema(store):
    required = {
        "feature_windows", "datasets", "model_versions",
        "model_evaluations", "shadow_predictions",
    }
    with store.connection() as db:
        present = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    missing = sorted(required - present)
    if missing:
        raise RuntimeError(
            "ThermoFlow SQLite migrations are not applied; missing tables: "
            + ", ".join(missing)
        )


def persist_burner_shadow_result(store, observed_report, benchmark, artifact,
                                 *, recorded_at=None):
    """Persist a validated dataset, evaluated artifact and holdout labels idempotently."""
    _require_schema(store)
    if not isinstance(observed_report, dict) or observed_report.get("schema") != REPORT_SCHEMA:
        raise ValueError("unsupported observed-cycle report")
    if not isinstance(benchmark, dict) or benchmark.get("schema") != BENCHMARK_SCHEMA:
        raise ValueError("unsupported Burner benchmark")
    if benchmark.get("status") != "evaluated":
        return {"schema": SCHEMA, "status": "skipped",
                "reason": "benchmark_not_evaluated", "writes": 0}
    if not isinstance(artifact, dict):
        raise ValueError("Burner live artifact must be a JSON object")

    dataset = observed_report.get("dataset")
    if (not isinstance(dataset, dict)
            or dataset.get("status") != "ready_for_shadow_training"
            or dataset.get("feature_keys") != list(FEATURE_KEYS)
            or dataset.get("target_key") != TARGET_KEY):
        raise ValueError("observed report does not contain a ready Burner dataset")
    training, holdout = dataset.get("training"), dataset.get("holdout")
    if not isinstance(training, list) or not isinstance(holdout, list):
        raise ValueError("dataset partitions must be lists")
    if len(training) < 20 or len(holdout) < 5:
        raise ValueError("Burner dataset requires at least 20 training and 5 holdout rows")
    if (dataset.get("training_samples") != len(training)
            or dataset.get("holdout_samples") != len(holdout)):
        raise ValueError("dataset partition counts do not match the rows")

    source_sha = observed_report.get("input_sha256")
    if (not isinstance(source_sha, str) or len(source_sha) != 64
            or benchmark.get("source_snapshot_sha256") != source_sha):
        raise ValueError("source snapshot checksum mismatch")
    if artifact.get("source_snapshot_sha256") != source_sha:
        raise ValueError("artifact source snapshot checksum mismatch")

    clock = int(time.time()) if recorded_at is None else recorded_at
    if type(clock) is not int or clock <= 0:
        raise ValueError("recorded_at must be a positive Unix timestamp")
    validated_artifact = load_live_artifact(artifact, now=clock)
    model = validated_artifact["model"]
    if benchmark.get("model") != model:
        raise ValueError("artifact model does not match evaluated benchmark model")
    if (model["training_samples"] != len(training)
            or model["holdout_samples"] != len(holdout)):
        raise ValueError("model sample counts do not match the dataset partitions")

    evaluation = benchmark.get("evaluation")
    candidate = evaluation.get("candidate") if isinstance(evaluation, dict) else None
    if (not isinstance(candidate, dict)
            or candidate.get("beats_both_baselines_on_mae") is not True
            or candidate.get("samples") != len(holdout)):
        raise ValueError("benchmark did not pass the untouched holdout gate")

    rows = []
    previous = 0
    for partition, partition_rows in (("training", training), ("holdout", holdout)):
        for row in partition_rows:
            if not isinstance(row, dict) or not isinstance(row.get("features"), dict):
                raise ValueError("malformed Burner dataset row")
            stamp = row.get("timestamp")
            target = row.get("target")
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or stamp <= previous):
                raise ValueError("dataset rows must be strictly chronological and finite")
            if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
                raise ValueError("cycle duration target must be positive and finite")
            features = row["features"]
            if set(features) != set(FEATURE_KEYS):
                raise ValueError("unexpected Burner feature keys")
            if (type(features["previous_duration_minutes"]) not in (int, float)
                    or not math.isfinite(features["previous_duration_minutes"])
                    or features["previous_duration_minutes"] <= 0
                    or type(features["idle_minutes"]) not in (int, float)
                    or not math.isfinite(features["idle_minutes"])
                    or features["idle_minutes"] < 0):
                raise ValueError("invalid Burner feature values")
            previous = stamp
            rows.append((partition, int(stamp), float(target), {
                key: float(features[key]) for key in FEATURE_KEYS
            }))

    dataset_content = {
        "source_snapshot_sha256": source_sha,
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "chronology": dataset.get("chronology"),
        "rows": [
            {"partition": part, "timestamp": stamp, "target": target,
             "features": features}
            for part, stamp, target, features in rows
        ],
    }
    dataset_version = _digest(dataset_content)
    artifact_sha = _digest(validated_artifact)
    model_version = f'{model.get("model_schema_version", "unknown")}+{artifact_sha[:16]}'
    manifest = {
        "schema": SCHEMA,
        "source": "fire_confirmed_complete_cycles",
        "source_snapshot_sha256": source_sha,
        "feature_set": FEATURE_SET,
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "training_samples": len(training),
        "holdout_samples": len(holdout),
        "eligible_samples": dataset.get("eligible_samples"),
        "chronology": dataset.get("chronology"),
        "dataset_content_sha256": dataset_version,
    }
    metadata = {
        "schema": SCHEMA,
        "mode": "read_only_shadow",
        "model_artifact": validated_artifact,
        "model_artifact_sha256": artifact_sha,
        "automatic_promotion": False,
    }
    metrics = {
        "schema": SCHEMA,
        "benchmark_schema": BENCHMARK_SCHEMA,
        "source_snapshot_sha256": source_sha,
        "evaluation": evaluation,
        "status": "evaluated",
        "promotion": "manual_review_required",
    }

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        dataset_cursor = db.execute(
            "INSERT OR IGNORE INTO datasets(name,version,created_at,manifest_json,"
            "content_sha256,status) VALUES(?,?,?,?,?,?)",
            (MODEL_NAME, dataset_version, clock, _canonical(manifest),
             dataset_version, "shadow"),
        )
        dataset_id = db.execute(
            "SELECT id FROM datasets WHERE name=? AND version=?",
            (MODEL_NAME, dataset_version),
        ).fetchone()["id"]

        feature_inserted = 0
        for partition, stamp, target, features in rows:
            values = {
                "partition": partition,
                "timestamp": stamp,
                "target": target,
                "features": features,
                "source_snapshot_sha256": source_sha,
            }
            cursor = db.execute(
                "INSERT OR IGNORE INTO feature_windows(feature_set,window_start,"
                "window_end,generated_at,dataset_version,values_json) "
                "VALUES(?,?,?,?,?,?)",
                (FEATURE_SET, stamp, stamp + 1, clock, dataset_version,
                 _canonical(values)),
            )
            feature_inserted += cursor.rowcount

        model_cursor = db.execute(
            "INSERT OR IGNORE INTO model_versions(model_name,version,created_at,"
            "artifact_path,artifact_sha256,metadata_json,status) VALUES(?,?,?,?,?,?,?)",
            (MODEL_NAME, model_version, validated_artifact["created_at"], None,
             artifact_sha, _canonical(metadata), "shadow"),
        )
        evaluation_exists = db.execute(
            "SELECT 1 FROM model_evaluations WHERE model_name=? AND model_version=? "
            "AND dataset_version=?",
            (MODEL_NAME, model_version, dataset_version),
        ).fetchone()
        evaluation_inserted = 0
        if evaluation_exists is None:
            db.execute(
                "INSERT INTO model_evaluations(model_name,model_version,dataset_version,"
                "evaluated_at,metrics_json,status) VALUES(?,?,?,?,?,?)",
                (MODEL_NAME, model_version, dataset_version, clock,
                 _canonical(metrics), "evaluated"),
            )
            evaluation_inserted = 1

        predictions_inserted = 0
        for row in holdout:
            stamp = int(row["timestamp"])
            entity_key = f"fire-cycle:{stamp}"
            existing = db.execute(
                "SELECT 1 FROM shadow_predictions WHERE model_name=? "
                "AND model_version=? AND entity_key=?",
                (MODEL_NAME, model_version, entity_key),
            ).fetchone()
            if existing is not None:
                continue
            predicted = predict(model, row["features"])["prediction"]
            target_at = int(stamp + float(row["target"]) * 60)
            prediction_json = _canonical({
                "value": predicted,
                "unit": "minutes",
                "features": row["features"],
                "mode": "offline_shadow_only",
                "source_snapshot_sha256": source_sha,
            })
            actual_json = _canonical({
                "value": row["target"],
                "unit": "minutes",
                "verified": True,
                "source": "fire_confirmed_complete_cycle",
            })
            db.execute(
                "INSERT INTO shadow_predictions(model_name,model_version,entity_key,"
                "predicted_at,target_at,prediction_json,actual_json,evaluation_status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (MODEL_NAME, model_version, entity_key, clock, target_at,
                 prediction_json, actual_json, "evaluated"),
            )
            predictions_inserted += 1

    return {
        "schema": SCHEMA,
        "status": "persisted",
        "dataset_id": int(dataset_id),
        "dataset_version": dataset_version,
        "model_version": model_version,
        "inserted": {
            "feature_windows": feature_inserted,
            "datasets": dataset_cursor.rowcount,
            "model_versions": model_cursor.rowcount,
            "model_evaluations": evaluation_inserted,
            "shadow_predictions": predictions_inserted,
        },
        "model_binary_stored": False,
        "raw_snapshot_stored": False,
        "raw_source_history_duplicated": False,
        "promotion": "manual_review_required",
    }


def load_burner_model_artifact(store, *, model_version=None, now=None):
    """Load the latest persisted Burner artifact and verify its content hash."""
    _require_schema(store)
    with store.connection() as db:
        if model_version is None:
            row = db.execute(
                "SELECT version,artifact_sha256,metadata_json FROM model_versions "
                "WHERE model_name=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (MODEL_NAME,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT version,artifact_sha256,metadata_json FROM model_versions "
                "WHERE model_name=? AND version=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (MODEL_NAME, model_version),
            ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError):
        raise ValueError("persisted Burner model metadata is invalid JSON") from None
    artifact = metadata.get("model_artifact") if isinstance(metadata, dict) else None
    expected = row["artifact_sha256"]
    if not isinstance(artifact, dict) or not isinstance(expected, str):
        raise ValueError("persisted Burner model artifact is missing")
    actual = _digest(artifact)
    if actual != expected or metadata.get("model_artifact_sha256") != expected:
        raise ValueError("persisted Burner model artifact checksum mismatch")
    validated = load_live_artifact(artifact, now=now)
    return {
        "model_name": MODEL_NAME,
        "model_version": row["version"],
        "artifact_sha256": expected,
        "artifact": validated,
        "mode": "read_only_shadow",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="existing migrated ThermoFlow SQLite database")
    parser.add_argument("observed_report", type=Path)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args(argv)
    try:
        database = Path(args.database).expanduser()
        if not database.is_file():
            raise FileNotFoundError("ThermoFlow SQLite database does not exist")
        observed = json.loads(args.observed_report.read_text(encoding="utf-8"))
        benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
        artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
        result = persist_burner_shadow_result(
            SQLiteStore(database), observed, benchmark, artifact
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"Burner SQLite import failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
