"""Strict artifact gate and inference contract for the read-only Burner ML model.

A benchmark is eligible for live shadow inference only when its untouched
chronological holdout beats both existing baselines. This module never promotes
a model automatically, writes files, or interacts with boiler controls.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from thermoflow_burner_ml import BENCHMARK_SCHEMA
from thermoflow_ml_burner_adapter import FEATURE_KEYS, TARGET_KEY
from thermoflow_ml_training import MODEL_SCHEMA_VERSION, predict

ARTIFACT_SCHEMA = "thermoflow-burner-live-model/1"
MAX_ARTIFACT_AGE_SECONDS = 30 * 86400
MAX_FUTURE_SKEW_SECONDS = 300


def _number(value, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("model contains a non-finite numeric value")
    if positive and value <= 0:
        raise ValueError("model value must be positive")
    if nonnegative and value < 0:
        raise ValueError("model value must be nonnegative")
    return float(value)


def _validated_model(model):
    if not isinstance(model, dict):
        raise ValueError("benchmark model is missing")
    if model.get("model_schema_version") != MODEL_SCHEMA_VERSION:
        raise ValueError("unsupported model schema")
    if model.get("mode") != "read_only_shadow":
        raise ValueError("model is not read-only shadow")
    if model.get("algorithm") != "ordinary_least_squares":
        raise ValueError("unsupported Burner ML algorithm")
    if model.get("feature_keys") != list(FEATURE_KEYS) or model.get("target_key") != TARGET_KEY:
        raise ValueError("unexpected Burner ML feature contract")
    if type(model.get("training_samples")) is not int or model["training_samples"] < 20:
        raise ValueError("insufficient model training samples")
    if type(model.get("holdout_samples")) is not int or model["holdout_samples"] < 5:
        raise ValueError("insufficient model holdout samples")
    means, scales, coefficients = (
        model.get("feature_means"), model.get("feature_scales"), model.get("coefficients")
    )
    if not all(isinstance(values, list) and len(values) == len(FEATURE_KEYS)
               for values in (means, scales, coefficients)):
        raise ValueError("invalid model vector dimensions")
    clean = {
        "model_schema_version": MODEL_SCHEMA_VERSION,
        "dataset_schema_version": model.get("dataset_schema_version"),
        "algorithm": "ordinary_least_squares",
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "feature_means": [_number(value) for value in means],
        "feature_scales": [_number(value, positive=True) for value in scales],
        "intercept": _number(model.get("intercept")),
        "coefficients": [_number(value) for value in coefficients],
        "training_samples": model["training_samples"],
        "holdout_samples": model["holdout_samples"],
        "mode": "read_only_shadow",
        "safety": "trained_on_training_partition_only_no_production_effect",
    }
    return clean


def build_live_artifact(benchmark, *, created_at=None):
    """Create a minimal inference artifact only after both holdout baselines win."""
    if not isinstance(benchmark, dict) or benchmark.get("schema") != BENCHMARK_SCHEMA:
        raise ValueError(f"expected {BENCHMARK_SCHEMA} benchmark")
    if benchmark.get("status") != "evaluated":
        raise ValueError("benchmark is not evaluated")
    evaluation = benchmark.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("benchmark evaluation is missing")
    candidate = evaluation.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("beats_both_baselines_on_mae") is not True:
        raise ValueError("candidate must beat both baselines on untouched holdout MAE")
    if candidate.get("samples") != evaluation.get("holdout_samples"):
        raise ValueError("candidate holdout count mismatch")
    for key in ("mae_minutes", "rmse_minutes", "bias_minutes"):
        _number(candidate.get(key), nonnegative=(key != "bias_minutes"))
    model = _validated_model(benchmark.get("model"))
    stamp = int(time.time() if created_at is None else created_at)
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("created_at must be a positive Unix timestamp")
    return {
        "schema": ARTIFACT_SCHEMA,
        "created_at": stamp,
        "mode": "read_only_shadow",
        "promotion": "manual_review_required",
        "source": "fire_confirmed_complete_cycles",
        "source_snapshot_sha256": benchmark.get("source_snapshot_sha256"),
        "training_samples": model["training_samples"],
        "holdout_samples": model["holdout_samples"],
        "holdout": {
            "candidate": {key: candidate[key] for key in
                          ("samples", "mae_minutes", "rmse_minutes", "bias_minutes")},
            "baselines": evaluation.get("baselines"),
        },
        "model": model,
        "safety": "prediction_only_no_ignition_inference_no_fuel_estimate_no_controller_effect",
    }


def load_live_artifact(value, *, now=None):
    """Validate artifact schema, model contract, and freshness before inference."""
    if isinstance(value, (bytes, bytearray)):
        if len(value) > 16 * 1024:
            raise ValueError("model artifact exceeds 16 KiB")
        value = json.loads(value)
    if not isinstance(value, dict) or value.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("invalid Burner ML artifact schema")
    stamp = value.get("created_at")
    clock = time.time() if now is None else now
    if type(stamp) is not int or stamp <= 0 or type(clock) not in (int, float) or not math.isfinite(clock):
        raise ValueError("invalid artifact timestamp")
    age = clock - stamp
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("model artifact is from the future")
    if age > MAX_ARTIFACT_AGE_SECONDS:
        raise ValueError("model artifact is stale")
    if value.get("mode") != "read_only_shadow" or value.get("promotion") != "manual_review_required":
        raise ValueError("artifact is not marked for manual shadow review")
    model = _validated_model(value.get("model"))
    if value.get("training_samples") != model["training_samples"] or value.get("holdout_samples") != model["holdout_samples"]:
        raise ValueError("artifact sample counts do not match model")
    return {**value, "model": model}


def predict_cycle_duration(artifact, *, previous_duration_minutes, idle_minutes):
    """Predict one FIRE-on cycle duration from features available at cycle start."""
    model = artifact.get("model") if isinstance(artifact, dict) else None
    validated = _validated_model(model)
    previous = _number(previous_duration_minutes, positive=True)
    idle = _number(idle_minutes, nonnegative=True)
    result = predict(validated, {
        "previous_duration_minutes": previous,
        "idle_minutes": idle,
    })
    estimate = _number(result.get("prediction"), positive=True)
    return {
        "status": "predicted",
        "prediction": round(estimate, 2),
        "unit": "minutes",
        "target": TARGET_KEY,
        "features": {
            "previous_duration_minutes": previous,
            "idle_minutes": idle,
        },
        "mode": "read_only_shadow",
        "safety": "estimate_only_no_ignition_or_controller_action",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path, help="evaluated thermoflow-burner-shadow/1 JSON")
    args = parser.parse_args(argv)
    try:
        benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
        artifact = build_live_artifact(benchmark)
        print(json.dumps(artifact, allow_nan=False, indent=2))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        print(f"Burner model artifact rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
