"""Persist verified Fuel ML datasets, artifacts and holdout outcomes in SQLite.

This is an explicit offline import. It never fetches refill data, infers fill
levels, runs from the API scheduler, promotes a model, or controls heating.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

from thermoflow_fuel_live import build_live_artifact, load_live_artifact
from thermoflow_fuel_ml import (
    AS_ISSUED, FEATURE_KEYS, INPUT_SCHEMA, REPORT_SCHEMA, TARGET_KEY,
)
from thermoflow_ml_training import predict
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

SCHEMA = "thermoflow-fuel-artifact-store/1"
MODEL_NAME = "fuel_rate"
FEATURE_SET = "fuel_verified_refill_v1"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("expected an ISO timestamp with timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    stamp = parsed.timestamp()
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("invalid source timestamp")
    return stamp


def _require_schema(store):
    required = {"feature_windows", "datasets", "model_versions",
                "model_evaluations", "shadow_predictions"}
    with store.connection() as db:
        present = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("ThermoFlow SQLite migrations are not applied; missing tables: "
                           + ", ".join(missing))


def _period_index(snapshot):
    periods = snapshot.get("periods")
    epochs = snapshot.get("calibration_epochs")
    if not isinstance(periods, list) or not isinstance(epochs, list):
        raise ValueError("verified refill periods and calibration epochs are required")
    boundaries = sorted(_instant(value) for value in epochs)
    by_start, by_end = {}, {}
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError("malformed verified refill period")
        start, end = _instant(period.get("start")), _instant(period.get("end"))
        if start >= end or start in by_start or end in by_end:
            raise ValueError("invalid or duplicate verified refill interval")
        if period.get("mass_reference") != "verified_equal_fill_level":
            continue
        mass = period.get("refilled_kg")
        baseline = period.get("baseline_kg_per_day")
        issued = _instant(period.get("baseline_issued_at"))
        if (type(mass) not in (int, float) or not math.isfinite(mass) or mass <= 0
                or type(baseline) not in (int, float) or not math.isfinite(baseline) or baseline <= 0
                or issued > start):
            continue
        rate = float(mass) / ((end - start) / 86400.0)
        if not math.isfinite(rate) or rate <= 0:
            continue
        entry = {"source": period, "start": start, "end": end,
                 "rate": rate, "baseline": float(baseline),
                 "regime": bisect_right(boundaries, start)}
        by_start[start] = entry
        by_end[end] = entry
    return by_start, by_end


def _verified_rows(dataset, snapshot):
    training, holdout = dataset.get("training"), dataset.get("holdout")
    if not isinstance(training, list) or not isinstance(holdout, list):
        raise ValueError("Fuel dataset partitions must be lists")
    if len(training) < 20 or len(holdout) < 5:
        raise ValueError("Fuel dataset requires at least 20 training and 5 holdout rows")
    if (dataset.get("training_samples") != len(training)
            or dataset.get("holdout_samples") != len(holdout)):
        raise ValueError("Fuel dataset partition counts do not match rows")

    by_start, by_end = _period_index(snapshot)
    rows, previous_stamp = [], 0
    for partition, partition_rows in (("training", training), ("holdout", holdout)):
        for row in partition_rows:
            if not isinstance(row, dict) or not isinstance(row.get("features"), dict):
                raise ValueError("malformed Fuel dataset row")
            stamp, target = row.get("timestamp"), row.get("target")
            features = row["features"]
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or stamp <= previous_stamp or type(target) not in (int, float)
                    or not math.isfinite(target) or target <= 0
                    or set(features) != set(FEATURE_KEYS)):
                raise ValueError("Fuel dataset rows must be finite and strictly chronological")
            current = by_start.get(float(stamp))
            prior = by_end.get(float(stamp))
            if current is None or prior is None or prior["regime"] != current["regime"]:
                raise ValueError("Fuel sample lacks adjacent verified periods in the same calibration regime")
            if not math.isclose(float(target), current["rate"], rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError("Fuel target does not match verified refill mass balance")
            if not math.isclose(float(features[FEATURE_KEYS[0]]), current["baseline"],
                                rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError("Fuel baseline feature does not match the as-issued source")
            if not math.isclose(float(features[FEATURE_KEYS[1]]), prior["rate"],
                                rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError("Fuel previous-rate feature does not match the adjacent verified period")
            previous_stamp = float(stamp)
            rows.append((partition, int(stamp), int(current["end"]),
                         float(target), {key: float(features[key]) for key in FEATURE_KEYS}))
    return rows


def persist_fuel_shadow_result(store, source_snapshot, benchmark, *, recorded_at=None):
    """Persist a verified Fuel dataset, evaluated artifact and holdout outcomes idempotently."""
    _require_schema(store)
    if not isinstance(source_snapshot, dict) or source_snapshot.get("schema") != INPUT_SCHEMA:
        raise ValueError("unsupported verified Fuel source snapshot")
    if (source_snapshot.get("baseline_mode") != AS_ISSUED
            or source_snapshot.get("label_mode") != "verified_equal_fill_level"):
        raise ValueError("Fuel SQLite import requires verified labels and as-issued baselines")
    if not isinstance(benchmark, dict) or benchmark.get("schema") != REPORT_SCHEMA:
        raise ValueError("unsupported Fuel benchmark")
    if benchmark.get("status") != "evaluated":
        return {"schema": SCHEMA, "status": "skipped",
                "reason": "benchmark_not_evaluated", "writes": 0}
    if (benchmark.get("mode") != "offline_shadow_only"
            or benchmark.get("promotion") != "disabled"
            or benchmark.get("baseline_mode") != AS_ISSUED
            or benchmark.get("label_mode") != "verified_equal_fill_level"):
        raise ValueError("Fuel benchmark provenance or shadow-only mode is invalid")
    dataset = benchmark.get("dataset")
    if (not isinstance(dataset, dict)
            or dataset.get("status") != "ready_for_shadow_training"
            or dataset.get("feature_keys") != list(FEATURE_KEYS)
            or dataset.get("target_key") != TARGET_KEY):
        raise ValueError("Fuel benchmark does not contain a ready verified dataset")

    clock = int(time.time()) if recorded_at is None else recorded_at
    if type(clock) is not int or clock <= 0:
        raise ValueError("recorded_at must be a positive Unix timestamp")
    artifact = build_live_artifact(benchmark, source_snapshot, created_at=clock)
    validated = load_live_artifact(artifact, now=clock)
    rows = _verified_rows(dataset, source_snapshot)
    source_sha = _digest(source_snapshot)
    artifact_sha = _digest(validated)
    model = validated["model"]
    model_identity_sha = _digest({
        "model": model, "source_snapshot_sha256": source_sha,
        "holdout": validated["holdout"],
    })
    model_version = f'{model["model_schema_version"]}+{model_identity_sha[:16]}'
    dataset_content = {
        "source_snapshot_sha256": source_sha,
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "baseline_mode": AS_ISSUED,
        "label_mode": "verified_equal_fill_level",
        "rows": [
            {"partition": part, "timestamp": stamp, "target_at": target_at,
             "target": target, "features": features}
            for part, stamp, target_at, target, features in rows
        ],
    }
    dataset_version = _digest(dataset_content)
    manifest = {
        "schema": SCHEMA, "source": "verified_refill_intervals_as_issued",
        "source_snapshot_sha256": source_sha, "feature_set": FEATURE_SET,
        "feature_keys": list(FEATURE_KEYS), "target_key": TARGET_KEY,
        "training_samples": len(dataset["training"]),
        "holdout_samples": len(dataset["holdout"]),
        "eligible_samples": dataset.get("eligible_samples"),
        "baseline_mode": AS_ISSUED,
        "label_mode": "verified_equal_fill_level",
        "dataset_content_sha256": dataset_version,
    }
    metadata = {
        "schema": SCHEMA, "mode": "read_only_shadow",
        "model_artifact": validated, "model_artifact_sha256": artifact_sha,
        "source_snapshot_sha256": source_sha, "automatic_promotion": False,
    }
    metrics = {
        "schema": SCHEMA, "benchmark_schema": REPORT_SCHEMA,
        "source_snapshot_sha256": source_sha,
        "evaluation": benchmark.get("evaluation"),
        "status": "evaluated", "promotion": "manual_review_required",
    }

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
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
        row_by_stamp = {stamp: (target_at, target, features)
                        for _, stamp, target_at, target, features in rows}
        for partition, stamp, target_at, target, features in rows:
            values = {"partition": partition, "timestamp": stamp,
                      "target_at": target_at, "target": target,
                      "features": features, "source_snapshot_sha256": source_sha}
            cursor = db.execute(
                "INSERT OR IGNORE INTO feature_windows(feature_set,window_start,"
                "window_end,generated_at,dataset_version,values_json) VALUES(?,?,?,?,?,?)",
                (FEATURE_SET, stamp, target_at, clock, dataset_version, _canonical(values)),
            )
            feature_inserted += cursor.rowcount

        model_exists = db.execute(
            "SELECT 1 FROM model_versions WHERE model_name=? AND version=?",
            (MODEL_NAME, model_version),
        ).fetchone()
        if model_exists is None:
            db.execute(
                "INSERT INTO model_versions(model_name,version,created_at,"
                "artifact_path,artifact_sha256,metadata_json,status) VALUES(?,?,?,?,?,?,?)",
                (MODEL_NAME, model_version, validated["created_at"], None,
                 artifact_sha, _canonical(metadata), "shadow"),
            )
            model_inserted = 1
        else:
            # Same evaluated model and source data: refresh its signed-off artifact
            # timestamp/checksum without creating a second model version.
            db.execute(
                "UPDATE model_versions SET created_at=?,artifact_sha256=?,metadata_json=?,status='shadow' "
                "WHERE model_name=? AND version=?",
                (validated["created_at"], artifact_sha, _canonical(metadata),
                 MODEL_NAME, model_version),
            )
            model_inserted = 0
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
        for row in dataset["holdout"]:
            stamp = int(row["timestamp"])
            target_at, target, features = row_by_stamp[stamp]
            entity_key = f"refill-period:{stamp}"
            exists = db.execute(
                "SELECT 1 FROM shadow_predictions WHERE model_name=? AND model_version=? AND entity_key=?",
                (MODEL_NAME, model_version, entity_key),
            ).fetchone()
            if exists is not None:
                continue
            estimate = predict(model, features)["prediction"]
            prediction_json = _canonical({
                "value": estimate, "unit": "kg_per_day", "features": features,
                "mode": "offline_shadow_only", "source_snapshot_sha256": source_sha,
            })
            actual_json = _canonical({
                "value": target, "unit": "kg_per_day", "verified": True,
                "source": "verified_equal_fill_level_refill_interval",
                "source_snapshot_sha256": source_sha,
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
        "schema": SCHEMA, "status": "persisted", "dataset_id": int(dataset_id),
        "dataset_version": dataset_version, "model_version": model_version,
        "inserted": {"feature_windows": feature_inserted,
                     "datasets": 1 if feature_inserted or evaluation_inserted or model_inserted else 0,
                     "model_versions": model_inserted,
                     "model_evaluations": evaluation_inserted,
                     "shadow_predictions": predictions_inserted},
        "promotion": "manual_review_required",
        "model_binary_stored": False, "raw_source_snapshot_duplicated": False,
    }


def load_fuel_model_artifact(store, *, model_version=None, now=None):
    """Load the latest checksum-verified Fuel artifact from SQLite."""
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
        raise ValueError("persisted Fuel model metadata is invalid JSON") from None
    artifact = metadata.get("model_artifact") if isinstance(metadata, dict) else None
    expected = row["artifact_sha256"]
    if not isinstance(artifact, dict) or not isinstance(expected, str):
        raise ValueError("persisted Fuel model artifact is missing")
    if _digest(artifact) != expected or metadata.get("model_artifact_sha256") != expected:
        raise ValueError("persisted Fuel model artifact checksum mismatch")
    validated = load_live_artifact(artifact, now=now)
    return {"model_name": MODEL_NAME, "model_version": row["version"],
            "artifact_sha256": expected, "artifact": validated,
            "mode": "read_only_shadow"}
