"""Rolling-origin, season-aware validation for ThermoFlow shadow ML.

This evaluator never persists or promotes a model and never touches production
fuel forecasts or heating controls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt

from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import train_and_evaluate

DEFAULT_MIN_TRAINING_SAMPLES = 40
DEFAULT_FOLDS = 5
DEFAULT_HOLDOUT_PER_FOLD = 5
DEFAULT_MIN_SPAN_DAYS = 60
DEFAULT_MIN_MONTHS = 3


def validate_rolling_origin(
    samples, *, feature_keys, target_key,
    minimum_training_samples=DEFAULT_MIN_TRAINING_SAMPLES,
    folds=DEFAULT_FOLDS,
    holdout_per_fold=DEFAULT_HOLDOUT_PER_FOLD,
    minimum_span_days=DEFAULT_MIN_SPAN_DAYS,
    minimum_months=DEFAULT_MIN_MONTHS,
):
    """Evaluate expanding-window folds and require meaningful time coverage."""
    if (type(minimum_training_samples) is not int or minimum_training_samples < 20
            or type(folds) is not int or folds < 2
            or type(holdout_per_fold) is not int or holdout_per_fold < 5
            or type(minimum_span_days) is not int or minimum_span_days < 1
            or type(minimum_months) is not int or minimum_months < 2):
        raise ValueError("invalid rolling-origin validation configuration")

    canonical = prepare_chronological_dataset(
        samples, feature_keys=feature_keys, target_key=target_key,
        minimum_training_samples=1, holdout_samples=1,
    )
    rows = canonical["training"] + canonical["holdout"]
    required = minimum_training_samples + folds * holdout_per_fold
    if len(rows) < required:
        return {
            "schema": "thermoflow-rolling-validation/1",
            "status": "collecting",
            "reason": "insufficient_eligible_samples",
            "eligible_samples": len(rows),
            "required_samples": required,
            "folds": [],
            "rejected_samples": canonical["rejected_samples"],
            "mode": "read_only_shadow",
            "safety": "validation_only_no_model_promotion_or_production_effect",
        }

    first_ts, last_ts = rows[0]["timestamp"], rows[-1]["timestamp"]
    span_days = (last_ts - first_ts) / 86400
    months = sorted({
        datetime.fromtimestamp(row["timestamp"], timezone.utc).strftime("%Y-%m")
        for row in rows
    })
    coverage = {
        "span_days": round(span_days, 3),
        "distinct_months": len(months),
        "months": months,
        "minimum_span_days": minimum_span_days,
        "minimum_months": minimum_months,
        "span_satisfied": span_days >= minimum_span_days,
        "months_satisfied": len(months) >= minimum_months,
    }
    if not coverage["span_satisfied"] or not coverage["months_satisfied"]:
        return {
            "schema": "thermoflow-rolling-validation/1",
            "status": "collecting",
            "reason": "insufficient_seasonal_coverage",
            "eligible_samples": len(rows),
            "required_samples": required,
            "coverage": coverage,
            "folds": [],
            "rejected_samples": canonical["rejected_samples"],
            "mode": "read_only_shadow",
            "safety": "validation_only_no_model_promotion_or_production_effect",
        }

    fold_results = []
    model_abs_error = model_squared_error = 0.0
    baseline_abs_error = baseline_squared_error = 0.0
    evaluated = 0
    for index in range(folds):
        available_after_initial = len(rows) - minimum_training_samples - holdout_per_fold
        offset = (available_after_initial * index) // (folds - 1)
        train_count = minimum_training_samples + offset
        validation_end = train_count + holdout_per_fold
        training = rows[:train_count]
        holdout = rows[train_count:validation_end]
        dataset = {
            "schema_version": canonical["schema_version"],
            "status": "ready_for_shadow_training",
            "feature_keys": list(feature_keys),
            "target_key": target_key,
            "training": training,
            "holdout": holdout,
        }
        result = train_and_evaluate(dataset)
        if result.get("status") != "evaluated":
            return {
                "schema": "thermoflow-rolling-validation/1",
                "status": "blocked",
                "reason": "fold_training_or_evaluation_failed",
                "failed_fold": index + 1,
                "fold_status": result.get("status"),
                "coverage": coverage,
                "folds": fold_results,
                "mode": "read_only_shadow",
                "safety": "validation_only_no_model_promotion_or_production_effect",
            }
        metrics = result["evaluation"]
        n = metrics["holdout"]["samples"]
        model_mae, model_rmse = metrics["holdout"]["mae"], metrics["holdout"]["rmse"]
        baseline_mae = metrics["baseline"]["mae"]
        baseline_rmse = metrics["baseline"]["rmse"]
        model_abs_error += model_mae * n
        model_squared_error += model_rmse * model_rmse * n
        baseline_abs_error += baseline_mae * n
        baseline_squared_error += baseline_rmse * baseline_rmse * n
        evaluated += n
        fold_results.append({
            "fold": index + 1,
            "training_samples": len(training),
            "holdout_samples": n,
            "training_end_timestamp": training[-1]["timestamp"],
            "holdout_start_timestamp": holdout[0]["timestamp"],
            "holdout_end_timestamp": holdout[-1]["timestamp"],
            "model": {"mae": model_mae, "rmse": model_rmse},
            "baseline": {"mae": baseline_mae, "rmse": baseline_rmse},
            "beats_baseline": model_mae < baseline_mae,
        })

    model_mae = model_abs_error / evaluated
    baseline_mae = baseline_abs_error / evaluated
    model_rmse = sqrt(model_squared_error / evaluated)
    baseline_rmse = sqrt(baseline_squared_error / evaluated)
    return {
        "schema": "thermoflow-rolling-validation/1",
        "status": "evaluated",
        "mode": "read_only_shadow",
        "coverage": coverage,
        "eligible_samples": len(rows),
        "fold_count": len(fold_results),
        "holdout_samples_total": evaluated,
        "aggregate": {
            "model": {"mae": round(model_mae, 6), "rmse": round(model_rmse, 6)},
            "baseline": {"algorithm": "training_target_median",
                         "mae": round(baseline_mae, 6), "rmse": round(baseline_rmse, 6)},
            "holdout_mae_improvement": round(baseline_mae - model_mae, 6),
            "beats_baseline": model_mae < baseline_mae,
        },
        "folds": fold_results,
        "rejected_samples": canonical["rejected_samples"],
        "chronology": "expanding_training_window_then_next_unseen_block",
        "safety": "validation_only_no_model_promotion_or_production_effect",
    }
