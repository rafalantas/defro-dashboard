"""Season-aware rolling validation for the actual Fibaro daily shadow model."""
from __future__ import annotations

from datetime import datetime, timezone

from thermoflow_fibaro_shadow_ml import (
    FEATURE_KEYS, TARGET_KEY, prepare_fibaro_daily_dataset,
)
from thermoflow_ml_validation import validate_rolling_origin


def _day_to_unix(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
        raise ValueError("Fibaro day timestamp must be YYYYMMDD")
    try:
        day = datetime.strptime(str(int(value)), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid Fibaro day timestamp") from None
    return day.timestamp()


def validate_fibaro_daily_rolling(
    daily_rows, quality_report, *, minimum_training_samples=40, folds=5,
    holdout_per_fold=5, minimum_span_days=60, minimum_months=3,
):
    """Validate Fibaro next-day heat-start predictions across expanding seasonal folds."""
    required_holdout = folds * holdout_per_fold
    dataset = prepare_fibaro_daily_dataset(
        daily_rows, quality_report,
        minimum_training_samples=minimum_training_samples,
        holdout_samples=required_holdout,
    )
    if dataset.get("status") != "ready_for_shadow_training":
        return {
            "schema": "thermoflow-fibaro-rolling-validation/1",
            "status": "collecting",
            "reason": dataset.get("reason", "fibaro_daily_dataset_not_ready"),
            "eligible_samples": dataset.get("eligible_samples", 0),
            "required_samples": minimum_training_samples + required_holdout,
            "mode": "offline_shadow_only",
            "promotion": "disabled",
        }

    samples = []
    for row in dataset["training"] + dataset["holdout"]:
        samples.append({
            "timestamp": _day_to_unix(row["timestamp"]),
            "complete": True,
            **row["features"],
            TARGET_KEY: row["target"],
        })
    result = validate_rolling_origin(
        samples, feature_keys=FEATURE_KEYS, target_key=TARGET_KEY,
        minimum_training_samples=minimum_training_samples,
        folds=folds, holdout_per_fold=holdout_per_fold,
        minimum_span_days=minimum_span_days, minimum_months=minimum_months,
    )
    result["schema"] = "thermoflow-fibaro-rolling-validation/1"
    result["task"] = "next_day_heat_required_starts"
    result["source"] = dataset.get("source")
    result["feature_semantics"] = dataset.get("feature_semantics")
    result["target_semantics"] = dataset.get("target_semantics")
    result["promotion"] = "disabled"
    result["mode"] = "offline_shadow_only"
    return result
