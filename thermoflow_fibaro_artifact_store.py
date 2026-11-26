"""Compact, idempotent persistence for evaluated Fibaro shadow artifacts."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thermoflow_ml_training import predict

SCHEMA = "thermoflow-fibaro-artifact-store/1"
MODEL_NAME = "fibaro_daily_heat_demand"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def persist_fibaro_shadow_result(store, result, *, recorded_at=None):
    """Persist compact references and outputs only after a completed shadow evaluation.

    Raw HC3 events remain in fibaro_events. Daily features are stored once in
    feature_windows; the dataset manifest references their deterministic version.
    Model binaries are not copied into SQLite.
    """
    if not isinstance(result, dict) or result.get("schema") != "thermoflow-fibaro-shadow-pipeline/1":
        raise ValueError("unsupported Fibaro pipeline result")
    if result.get("status") != "evaluated":
        return {"status": "skipped", "reason": "pipeline_not_evaluated", "writes": 0}
    quality = result.get("quality")
    model_result = result.get("model")
    backtest = result.get("backtest")
    daily_rows = result.get("daily_features")
    window = result.get("window")
    if (not isinstance(quality, dict) or quality.get("status") != "ready"
            or not isinstance(model_result, dict) or model_result.get("status") != "evaluated"
            or not isinstance(model_result.get("model"), dict)
            or not isinstance(model_result.get("dataset"), dict)
            or not isinstance(backtest, dict) or backtest.get("status") != "evaluated"
            or not isinstance(daily_rows, list) or not daily_rows
            or not isinstance(window, dict)):
        return {"status": "skipped", "reason": "quality_model_or_backtest_not_ready", "writes": 0}

    try:
        zone = ZoneInfo(window.get("timezone", "Europe/Warsaw"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid Fibaro artifact timezone") from None
    clock = int(datetime.now().timestamp()) if recorded_at is None else recorded_at
    if type(clock) is not int or clock <= 0:
        raise ValueError("recorded_at must be a positive Unix timestamp")

    daily_payload = sorted(daily_rows, key=lambda row: row["date"])
    dataset_version = _digest(daily_payload)
    model = model_result["model"]
    model_sha = _digest(model)
    model_version = f"{model.get('model_schema_version', 'unknown')}+{model_sha[:16]}"
    quality_sha = _digest(quality)
    dataset_manifest = {
        "schema": SCHEMA,
        "source": "fibaro_hc3_persisted_daily_event_aggregates",
        "feature_set": "fibaro_daily_v1",
        "feature_window_count": len(daily_payload),
        "feature_window_version": dataset_version,
        "quality_report_sha256": quality_sha,
        "timezone": window.get("timezone", "Europe/Warsaw"),
        "window": {"start": window.get("start"), "end": window.get("end")},
        "storage_semantics": "feature_windows_are_canonical_daily_rows",
    }
    metadata = {
        "schema": SCHEMA,
        "mode": "offline_shadow_only",
        "feature_keys": model.get("feature_keys", []),
        "target_key": model.get("target_key"),
        "dataset_version": dataset_version,
        # Keep the complete deterministic, JSON-serializable model artifact
        # alongside its digest. SQLite stores metadata, not executable binaries.
        "model_artifact": model,
        "model_artifact_sha256": model_sha,
        "automatic_promotion": False,
    }
    metrics = {
        "schema": SCHEMA,
        "quality_status": quality["status"],
        "backtest_summary": backtest.get("summary", {}),
        "readiness": backtest.get("readiness", {}),
        "evaluation": model_result.get("evaluation"),
        "quality_report_sha256": quality_sha,
    }

    holdout = model_result["dataset"].get("holdout")
    if not isinstance(holdout, list):
        raise ValueError("evaluated Fibaro model has no holdout rows")

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO datasets(name,version,created_at,manifest_json,"
            "content_sha256,status) VALUES(?,?,?,?,?,?)",
            ("fibaro_daily_heat_demand", dataset_version, clock,
             _canonical(dataset_manifest), dataset_version, "shadow"),
        )
        dataset_id = db.execute(
            "SELECT id FROM datasets WHERE name=? AND version=?",
            ("fibaro_daily_heat_demand", dataset_version),
        ).fetchone()["id"]

        feature_inserted = 0
        for row in daily_payload:
            day = date.fromisoformat(row["date"])
            start_at = int(datetime.combine(day, time.min, tzinfo=zone).timestamp())
            end_at = int(datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).timestamp())
            cursor = db.execute(
                "INSERT OR IGNORE INTO feature_windows(feature_set,window_start,window_end,"
                "generated_at,dataset_version,values_json) VALUES(?,?,?,?,?,?)",
                ("fibaro_daily_v1", start_at, end_at, clock,
                 dataset_version, _canonical(row)),
            )
            feature_inserted += cursor.rowcount

        db.execute(
            "INSERT OR IGNORE INTO model_versions(model_name,version,created_at,artifact_path,"
            "artifact_sha256,metadata_json,status) VALUES(?,?,?,?,?,?,?)",
            (MODEL_NAME, model_version, clock, None, model_sha,
             _canonical(metadata), "shadow"),
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
                 _canonical(metrics), backtest.get("readiness", {}).get("status", "evaluated")),
            )
            evaluation_inserted = 1

        predictions_inserted = 0
        for row in holdout:
            day = datetime.strptime(str(row["timestamp"]), "%Y%m%d").date()
            predicted_at = int(datetime.combine(day, time.min, tzinfo=zone).timestamp())
            target_at = int(datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).timestamp())
            entity_key = f"local-day:{day.isoformat()}"
            existing = db.execute(
                "SELECT 1 FROM shadow_predictions WHERE model_name=? AND model_version=? "
                "AND entity_key=? AND predicted_at=?",
                (MODEL_NAME, model_version, entity_key, predicted_at),
            ).fetchone()
            if existing is not None:
                continue
            predicted = predict(model, row["features"])["prediction"]
            prediction_json = _canonical({"value": predicted, "mode": "offline_shadow_only"})
            actual_json = _canonical({"value": row["target"], "verified": True})
            db.execute(
                "INSERT INTO shadow_predictions(model_name,model_version,entity_key,"
                "predicted_at,target_at,prediction_json,actual_json,evaluation_status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (MODEL_NAME, model_version, entity_key, predicted_at, target_at,
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
            "model_evaluations": evaluation_inserted,
            "shadow_predictions": predictions_inserted,
        },
        "deduplication": "content_addressed_features_and_model; deterministic_holdout_keys",
        "model_binary_stored": False,
        "raw_source_history_duplicated": False,
    }


def load_fibaro_model_artifact(store, *, model_version=None):
    """Load and verify a persisted JSON model artifact without executing code."""
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
        raise ValueError("persisted Fibaro model metadata is invalid JSON") from None
    model = metadata.get("model_artifact") if isinstance(metadata, dict) else None
    expected = row["artifact_sha256"]
    if not isinstance(model, dict) or not isinstance(expected, str):
        raise ValueError("persisted Fibaro model artifact is missing")
    actual = _digest(model)
    if actual != expected or metadata.get("model_artifact_sha256") != expected:
        raise ValueError("persisted Fibaro model artifact checksum mismatch")
    if model.get("model_schema_version") != "1.0.0" or model.get("mode") != "read_only_shadow":
        raise ValueError("persisted Fibaro model artifact schema or mode is unsupported")
    return {
        "model_name": MODEL_NAME,
        "model_version": row["version"],
        "artifact_sha256": expected,
        "model": model,
        "mode": "offline_shadow_only",
    }
