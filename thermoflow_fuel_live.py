"""Verified Fuel ML artifact export and inference contract.

Only a benchmark trained from verified refill labels and as-issued baselines
that beats both fixed holdout comparators can produce a live shadow artifact.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

from thermoflow_fuel_ml import AS_ISSUED, INPUT_SCHEMA, REPORT_SCHEMA
from thermoflow_ml_training import MODEL_SCHEMA_VERSION, predict

ARTIFACT_SCHEMA = "thermoflow-fuel-live-model/1"
MAX_ARTIFACT_AGE_SECONDS = 30 * 86400
MAX_VERIFIED_PERIOD_AGE_SECONDS = 14 * 86400
FEATURE_KEYS = ("baseline_kg_per_day", "previous_kg_per_day")
TARGET_KEY = "observed_kg_per_day"


def _number(value, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("model contains a non-finite numeric value")
    if positive and value <= 0:
        raise ValueError("model value must be positive")
    if nonnegative and value < 0:
        raise ValueError("model value must be nonnegative")
    return float(value)


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("expected an ISO timestamp with timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.timestamp()


def _validated_model(model):
    if not isinstance(model, dict) or model.get("model_schema_version") != MODEL_SCHEMA_VERSION:
        raise ValueError("unsupported Fuel ML model schema")
    if model.get("mode") != "read_only_shadow" or model.get("algorithm") != "ordinary_least_squares":
        raise ValueError("unsupported Fuel ML model mode or algorithm")
    if model.get("feature_keys") != list(FEATURE_KEYS) or model.get("target_key") != TARGET_KEY:
        raise ValueError("unexpected Fuel ML feature contract")
    if type(model.get("training_samples")) is not int or model["training_samples"] < 20:
        raise ValueError("insufficient Fuel ML training samples")
    if type(model.get("holdout_samples")) is not int or model["holdout_samples"] < 5:
        raise ValueError("insufficient Fuel ML holdout samples")
    vectors = [model.get("feature_means"), model.get("feature_scales"), model.get("coefficients")]
    if not all(isinstance(vector, list) and len(vector) == len(FEATURE_KEYS) for vector in vectors):
        raise ValueError("invalid Fuel ML model dimensions")
    return {
        "model_schema_version": MODEL_SCHEMA_VERSION,
        "dataset_schema_version": model.get("dataset_schema_version"),
        "algorithm": "ordinary_least_squares",
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "feature_means": [_number(v) for v in vectors[0]],
        "feature_scales": [_number(v, positive=True) for v in vectors[1]],
        "intercept": _number(model.get("intercept")),
        "coefficients": [_number(v) for v in vectors[2]],
        "training_samples": model["training_samples"],
        "holdout_samples": model["holdout_samples"],
        "mode": "read_only_shadow",
        "safety": "trained_on_verified_refill_labels_no_production_effect",
    }


def build_live_artifact(benchmark, snapshot, *, created_at=None):
    """Export only if the candidate beats both baselines on the untouched holdout."""
    if not isinstance(benchmark, dict) or benchmark.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"expected {REPORT_SCHEMA} benchmark")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"expected {INPUT_SCHEMA} source snapshot")
    if snapshot.get("baseline_mode") != AS_ISSUED or snapshot.get("label_mode") != "verified_equal_fill_level":
        raise ValueError("Fuel ML live artifact requires verified labels and as-issued baselines")
    if benchmark.get("status") != "evaluated" or benchmark.get("model") is None:
        raise ValueError("Fuel ML benchmark is not evaluated")
    evaluation = benchmark.get("evaluation")
    candidate = evaluation.get("candidate") if isinstance(evaluation, dict) else None
    baseline = evaluation.get("as_issued_baseline") if isinstance(evaluation, dict) else None
    previous = evaluation.get("previous_period") if isinstance(evaluation, dict) else None
    if not all(isinstance(row, dict) for row in (candidate, baseline, previous)):
        raise ValueError("Fuel ML holdout comparisons are incomplete")
    for row in (candidate, baseline, previous):
        if row.get("samples") != 5:
            raise ValueError("Fuel ML requires five untouched holdout samples per comparator")
        _number(row.get("mae_kg_per_day"), nonnegative=True)
        _number(row.get("rmse_kg_per_day"), nonnegative=True)
    if not (candidate["mae_kg_per_day"] < baseline["mae_kg_per_day"]
            and candidate["mae_kg_per_day"] < previous["mae_kg_per_day"]):
        raise ValueError("Fuel ML candidate must beat both as-issued and previous-period baselines")

    dataset = benchmark.get("dataset")
    holdout = dataset.get("holdout") if isinstance(dataset, dict) else None
    if not isinstance(holdout, list) or len(holdout) < 5:
        raise ValueError("Fuel ML verified holdout rows are missing")
    latest = holdout[-1]
    latest_start = latest.get("timestamp")
    latest_rate = _number(latest.get("target"), positive=True)
    if type(latest_start) not in (int, float) or not math.isfinite(latest_start):
        raise ValueError("invalid latest verified period timestamp")
    latest_period = None
    for period in snapshot.get("periods", []):
        try:
            if abs(_instant(period["start"]) - latest_start) < 1:
                latest_period = period
                break
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if (not isinstance(latest_period, dict)
            or latest_period.get("mass_reference") != "verified_equal_fill_level"
            or not latest_period.get("baseline_issued_at")):
        raise ValueError("latest holdout period has no verified source provenance")
    period_start = _instant(latest_period.get("start"))
    if _instant(latest_period["baseline_issued_at"]) > period_start:
        raise ValueError("latest period baseline was issued after the period started")
    period_end = _instant(latest_period.get("end"))
    as_of = _instant(snapshot.get("as_of"))
    if period_end > as_of or as_of - period_end > MAX_VERIFIED_PERIOD_AGE_SECONDS:
        raise ValueError("latest verified refill period is stale or in the future")
    actual_rate = _number(latest_period.get("refilled_kg"), positive=True) / (
        (period_end - _instant(latest_period.get("start"))) / 86400
    )
    if not math.isclose(actual_rate, latest_rate, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("latest verified period does not match the evaluated holdout")

    model = _validated_model(benchmark["model"])
    stamp = int(time.time() if created_at is None else created_at)
    if type(stamp) is not int or stamp <= 0 or stamp < as_of:
        raise ValueError("created_at must be a positive timestamp after the source snapshot")
    return {
        "schema": ARTIFACT_SCHEMA,
        "created_at": stamp,
        "source_as_of": int(as_of),
        "latest_verified_period_end": int(period_end),
        "latest_verified_rate_kg_per_day": latest_rate,
        "mode": "read_only_shadow",
        "promotion": "manual_review_required",
        "source": "verified_refill_intervals_as_issued",
        "training_samples": model["training_samples"],
        "holdout_samples": model["holdout_samples"],
        "holdout": {
            "candidate": {key: candidate[key] for key in
                          ("samples", "mae_kg_per_day", "rmse_kg_per_day", "bias_kg_per_day")},
            "as_issued_baseline": {key: baseline[key] for key in
                                   ("samples", "mae_kg_per_day", "rmse_kg_per_day", "bias_kg_per_day")},
            "previous_period": {key: previous[key] for key in
                                ("samples", "mae_kg_per_day", "rmse_kg_per_day", "bias_kg_per_day")},
        },
        "model": model,
        "safety": "prediction_only_verified_labels_no_boiler_control_effect",
    }


def load_live_artifact(value, *, now=None):
    if isinstance(value, (bytes, bytearray)):
        if len(value) > 16 * 1024:
            raise ValueError("Fuel ML artifact exceeds 16 KiB")
        value = json.loads(value)
    if not isinstance(value, dict) or value.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("invalid Fuel ML artifact schema")
    clock = time.time() if now is None else now
    created, source_as_of = value.get("created_at"), value.get("source_as_of")
    period_end = value.get("latest_verified_period_end")
    if (type(clock) not in (int, float) or not math.isfinite(clock)
            or type(created) is not int or type(source_as_of) is not int
            or type(period_end) is not int):
        raise ValueError("invalid Fuel ML artifact timestamps")
    if clock < created - 300 or clock - created > MAX_ARTIFACT_AGE_SECONDS:
        raise ValueError("Fuel ML artifact is future-dated or stale")
    if not period_end <= source_as_of <= created or clock < period_end:
        raise ValueError("Fuel ML source timestamps are inconsistent")
    if clock - period_end > MAX_VERIFIED_PERIOD_AGE_SECONDS:
        raise ValueError("latest verified Fuel ML period is future-dated or stale")
    if value.get("mode") != "read_only_shadow" or value.get("promotion") != "manual_review_required":
        raise ValueError("Fuel ML artifact is not marked for manual shadow review")
    model = _validated_model(value.get("model"))
    if (value.get("training_samples") != model["training_samples"]
            or value.get("holdout_samples") != model["holdout_samples"]):
        raise ValueError("Fuel ML artifact sample counts do not match model")
    rate = _number(value.get("latest_verified_rate_kg_per_day"), positive=True)
    return {**value, "model": model, "latest_verified_rate_kg_per_day": rate}


def predict_fuel_rate(artifact, *, baseline_kg_per_day):
    model = _validated_model(artifact.get("model") if isinstance(artifact, dict) else None)
    baseline = _number(baseline_kg_per_day, positive=True)
    previous = _number(artifact.get("latest_verified_rate_kg_per_day"), positive=True)
    result = predict(model, {
        "baseline_kg_per_day": baseline,
        "previous_kg_per_day": previous,
    })
    estimate = _number(result.get("prediction"), positive=True)
    return {
        "status": "predicted",
        "value": round(estimate, 2),
        "unit": "kg/d",
        "baseline_kg_per_day": baseline,
        "previous_kg_per_day": previous,
        "latest_verified_period_end": artifact["latest_verified_period_end"],
        "mode": "read_only_shadow",
        "safety": "estimate_only_no_inventory_or_controller_action",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="verified thermoflow-verified-fuel-periods/1 JSON")
    parser.add_argument("benchmark", type=Path, help="evaluated thermoflow-fuel-shadow/1 JSON")
    args = parser.parse_args(argv)
    try:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
        artifact = build_live_artifact(benchmark, snapshot)
        print(json.dumps(artifact, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        print(f"Fuel ML model artifact rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
