"""Shadow-only Fibaro daily heating-demand model using existing ThermoFlow ML."""
from __future__ import annotations

from datetime import date, timedelta
from math import isfinite

from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import train_and_evaluate
from thermoflow_fibaro_data_quality import SCHEMA as QUALITY_SCHEMA

SCHEMA = "thermoflow-fibaro-shadow-ml/1"
FEATURE_KEYS = ("previous_day_heat_starts", "previous_day_window_opens",
                "previous_day_temperature_abs_delta_c")
TARGET_KEY = "next_day_heat_starts"
MIN_TRAINING_SAMPLES = 20
HOLDOUT_SAMPLES = 5


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def prepare_fibaro_daily_dataset(daily_rows, quality_report):
    """Create next-day event-count samples from consecutive observed local days.

    No absent days are synthesized. A pair is eligible only when both days have
    all required observed aggregates; this is event-count prediction, not a
    reconstruction of heat-on duration or room occupancy.
    """
    if not isinstance(quality_report, dict) or quality_report.get("schema") != QUALITY_SCHEMA:
        raise ValueError("a Fibaro data-quality report is required")
    if (quality_report.get("status") != "ready"
            or not isinstance(quality_report.get("training_gate"), dict)
            or quality_report["training_gate"].get("eligible") is not True):
        return {"schema_version": "1.0.0", "status": "collecting",
                "reason": "fibaro_quality_gate_not_ready", "training": [], "holdout": [],
                "eligible_samples": 0, "safety": "shadow_only"}

    if not isinstance(daily_rows, list):
        raise ValueError("daily_rows must be a list")
    by_date = {}
    rejected = {"malformed_day": 0, "duplicate_day": 0, "missing_feature": 0,
                "non_consecutive_day": 0}
    for row in daily_rows:
        if not isinstance(row, dict):
            rejected["malformed_day"] += 1
            continue
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            rejected["malformed_day"] += 1
            continue
        if day.isoformat() in by_date:
            rejected["duplicate_day"] += 1
            continue
        by_date[day.isoformat()] = row

    ordered = sorted(by_date.items())
    samples = []
    for (previous_key, previous), (current_key, current) in zip(ordered, ordered[1:]):
        previous_day = date.fromisoformat(previous_key)
        current_day = date.fromisoformat(current_key)
        if current_day != previous_day + timedelta(days=1):
            rejected["non_consecutive_day"] += 1
            continue
        values = (
            previous.get("heat_demand_started"),
            previous.get("window_opened"),
            previous.get("temperature_mean_absolute_delta_c"),
            current.get("heat_demand_started"),
        )
        if not all(_number(value) for value in values):
            rejected["missing_feature"] += 1
            continue
        if any(value < 0 for value in values):
            rejected["missing_feature"] += 1
            continue
        samples.append({
            "timestamp": int(current_day.strftime("%Y%m%d")),
            "complete": True,
            FEATURE_KEYS[0]: float(values[0]),
            FEATURE_KEYS[1]: float(values[1]),
            FEATURE_KEYS[2]: float(values[2]),
            TARGET_KEY: float(values[3]),
        })

    dataset = prepare_chronological_dataset(
        samples, feature_keys=FEATURE_KEYS, target_key=TARGET_KEY,
        minimum_training_samples=MIN_TRAINING_SAMPLES,
        holdout_samples=HOLDOUT_SAMPLES,
    )
    dataset["source"] = "fibaro_hc3_persisted_daily_event_aggregates"
    dataset["rejected_days_or_pairs"] = rejected
    dataset["feature_semantics"] = {
        FEATURE_KEYS[0]: "heatRequired rising transitions observed on the preceding local day",
        FEATURE_KEYS[1]: "windowOpened rising transitions observed on the preceding local day",
        FEATURE_KEYS[2]: "mean absolute temperature-change magnitude from clean observed transitions on the preceding local day",
    }
    dataset["target_semantics"] = "heatRequired rising transitions observed on the following local day"
    dataset["safety"] = "offline_shadow_only_no_state_duration_or_occupancy_inference"
    return dataset


def train_fibaro_daily_model(daily_rows, quality_report):
    """Train and evaluate on chronological partitions; never persist or promote."""
    dataset = prepare_fibaro_daily_dataset(daily_rows, quality_report)
    if dataset.get("status") != "ready_for_shadow_training":
        return {"schema": SCHEMA, "status": "collecting",
                "reason": dataset.get("reason", "insufficient_consecutive_daily_samples"),
                "dataset": dataset, "model": None, "evaluation": None,
                "promotion": "disabled", "mode": "offline_shadow_only"}
    try:
        trained = train_and_evaluate(dataset)
    except ValueError as error:
        if "linearly dependent" not in str(error):
            raise
        return {"schema": SCHEMA, "status": "unavailable",
                "reason": "non_identifiable_features", "dataset": dataset,
                "model": None, "evaluation": None, "promotion": "disabled",
                "mode": "offline_shadow_only"}
    return {"schema": SCHEMA, "status": trained["status"],
            "model_status": trained.get("model_status"),
            "dataset": dataset, "model": trained.get("model"),
            "evaluation": trained.get("evaluation"),
            "promotion": "disabled", "mode": "offline_shadow_only",
            "safety": "no_hc3_commands_no_boiler_control_no_fuel_forecast_changes"}
