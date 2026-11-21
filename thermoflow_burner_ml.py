"""Offline benchmark for the duration of an observed FIRE-confirmed burner cycle."""
from __future__ import annotations

import argparse
import json
import sys
from math import isfinite, sqrt
from pathlib import Path
from statistics import median

from thermoflow_ml_burner_adapter import FEATURE_KEYS, TARGET_KEY
from thermoflow_ml_training import predict, train_and_evaluate
from thermoflow_observed_cycles import REPORT_SCHEMA

BENCHMARK_SCHEMA = "thermoflow-burner-shadow/1"


def _metrics(actual, estimates):
    errors = [estimate - target for estimate, target in zip(estimates, actual, strict=True)]
    return {
        "samples": len(errors),
        "mae_minutes": sum(abs(error) for error in errors) / len(errors),
        "rmse_minutes": sqrt(sum(error * error for error in errors) / len(errors)),
        "bias_minutes": sum(errors) / len(errors),
    }


def evaluate_burner_model(observed_report: dict) -> dict:
    """Evaluate on a fixed holdout, with no selection based on holdout data."""
    if not isinstance(observed_report, dict) or observed_report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"expected {REPORT_SCHEMA} report")
    dataset = observed_report.get("dataset")
    if (not isinstance(dataset, dict) or dataset.get("feature_keys") != list(FEATURE_KEYS)
            or dataset.get("target_key") != TARGET_KEY):
        raise ValueError("expected the observed burner cycle feature contract")

    report = {
        "schema": BENCHMARK_SCHEMA,
        "source_snapshot_sha256": observed_report.get("input_sha256"),
        "mode": "offline_shadow_only",
        "promotion": "disabled",
        "eligible_samples": dataset.get("eligible_samples"),
        "prediction_available_at": "after_first_two_contiguous_fire_on_samples",
        "model": None,
        "evaluation": None,
    }
    training = dataset.get("training")
    holdout = dataset.get("holdout")
    if not isinstance(training, list) or not isinstance(holdout, list):
        raise ValueError("invalid training or holdout partition")

    for row in training + holdout:
        target = row["target"]
        features = row["features"]
        numbers = (target, *(features[key] for key in FEATURE_KEYS))
        if not all(type(value) in (int, float) and isfinite(value) for value in numbers):
            raise ValueError("cycle durations and idle time must be finite numbers")
        if target <= 0 or features["previous_duration_minutes"] <= 0 or features["idle_minutes"] < 0:
            raise ValueError("cycle duration must be positive and idle time nonnegative")

    try:
        trained = train_and_evaluate(dataset)
    except ValueError as error:
        if str(error) != "features are linearly dependent; model is not identifiable":
            raise
        trained = {"status": "unavailable", "reason": "degenerate_training_features", "model": None}
    if trained["status"] == "collecting":
        return {**report, "status": "collecting", "reason": trained["reason"]}

    actual = [row["target"] for row in holdout]
    persistence = [row["features"]["previous_duration_minutes"] for row in holdout]
    center = median(row["target"] for row in training)
    baselines = {
        "training_median": _metrics(actual, [center] * len(holdout)),
        "previous_cycle": _metrics(actual, persistence),
    }
    report["evaluation"] = {
        "training_samples": len(training),
        "holdout_samples": len(holdout),
        "training_ends_at": training[-1]["timestamp"],
        "holdout_starts_at": holdout[0]["timestamp"],
        "baselines": baselines,
        "candidate": None,
    }
    if trained["model"] is None:
        return {**report, "status": "unavailable", "reason": trained["reason"]}

    estimates = [predict(trained["model"], row["features"])["prediction"] for row in holdout]
    if any(not isfinite(estimate) or estimate <= 0 for estimate in estimates):
        return {**report, "status": "unavailable", "reason": "nonphysical_holdout_prediction"}

    scores = _metrics(actual, estimates)
    report["evaluation"]["candidate"] = {
        "algorithm": trained["model"]["algorithm"], **scores,
        "beats_both_baselines_on_mae": all(
            scores["mae_minutes"] < baseline["mae_minutes"] for baseline in baselines.values()),
    }
    return {**report, "status": "evaluated", "model": trained["model"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="local thermoflow-observed-cycles report")
    args = parser.parse_args(argv)
    try:
        result = evaluate_burner_model(json.loads(args.report.read_text(encoding="utf-8")))
        encoded = json.dumps(result, allow_nan=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f"Burner benchmark failed: {error}", file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
