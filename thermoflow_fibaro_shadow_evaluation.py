"""Chronological backtesting and readiness gates for Fibaro daily shadow ML."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from statistics import median

from thermoflow_ml_training import predict
from thermoflow_shadow_evaluation import evaluate_shadow

SCHEMA = "thermoflow-fibaro-shadow-backtest/1"
MODEL_SCHEMA = "1.0.0"


def _day_timestamp(value):
    if (isinstance(value, bool) or type(value) not in (int, float)
            or not isfinite(value) or not float(value).is_integer()):
        raise ValueError("dataset day timestamp must be an integer YYYYMMDD")
    try:
        parsed = datetime.strptime(str(int(value)), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid dataset day timestamp") from None
    return int(parsed.timestamp())


def evaluate_fibaro_shadow(model_result):
    """Build an auditable daily ledger and evaluate only the untouched holdout."""
    if not isinstance(model_result, dict):
        raise ValueError("model_result must be a mapping")
    dataset = model_result.get("dataset")
    model = model_result.get("model")
    if model_result.get("status") != "evaluated" or not isinstance(dataset, dict) or not isinstance(model, dict):
        return {"schema": SCHEMA, "status": "collecting",
                "reason": "trained_shadow_model_and_holdout_required",
                "backtest": None, "readiness": {"status": "not_ready"},
                "promotion": "disabled", "mode": "offline_shadow_only"}
    evaluation = model_result.get("evaluation")
    if (model.get("model_schema_version") != MODEL_SCHEMA
            or model.get("mode") != "read_only_shadow"
            or not isinstance(evaluation, dict)
            or evaluation.get("holdout_is_unseen") is not True):
        raise ValueError("model must be a validated read-only model with an unseen holdout")

    training = dataset.get("training")
    holdout = dataset.get("holdout")
    if not isinstance(training, list) or not isinstance(holdout, list) or len(training) < 20:
        raise ValueError("chronological training and holdout partitions are required")
    if len(holdout) < 5:
        return {"schema": SCHEMA, "status": "collecting",
                "reason": "minimum_five_holdout_days_required",
                "backtest": None, "readiness": {"status": "not_ready"},
                "promotion": "disabled", "mode": "offline_shadow_only"}

    baseline_value = median(float(row["target"]) for row in training)
    cases = []
    previous_decision = None
    for row in holdout:
        if not isinstance(row, dict) or not isinstance(row.get("features"), dict):
            raise ValueError("malformed holdout row")
        decision = _day_timestamp(row.get("timestamp"))
        outcome_end = decision + 86400
        if previous_decision is not None and decision <= previous_decision:
            raise ValueError("holdout must be strictly chronological")
        previous_decision = decision
        actual = row.get("target")
        if type(actual) not in (int, float) or not isfinite(actual) or actual < 0:
            raise ValueError("Fibaro event-count labels must be finite and nonnegative")
        candidate = predict(model, row["features"])["prediction"]
        if not isfinite(candidate) or candidate < 0:
            raise ValueError("candidate prediction must be finite and nonnegative")
        cases.append({
            "id": f"fibaro-day-{row['timestamp']}",
            "decision_at": decision,
            "outcome_end": outcome_end,
            "label": {"value": float(actual), "verified": True, "available_at": outcome_end},
            "baseline": {"value": baseline_value, "issued_at": decision,
                         "data_cutoff": decision, "duration_ms": 0},
            "candidate": {"value": candidate, "issued_at": decision,
                          "data_cutoff": decision, "duration_ms": 0},
        })

    start = cases[0]["decision_at"]
    end = cases[-1]["decision_at"] + 1
    as_of = cases[-1]["outcome_end"]
    ledger = {
        "schema": "thermoflow-shadow-ledger/1",
        "task": "fibaro_next_day_heat_required_starts",
        "kind": "regression",
        "start": start,
        "end": end,
        "as_of": as_of,
        "window_seconds": 86400,
        "cases": cases,
    }
    report = evaluate_shadow(ledger)

    candidate_errors, baseline_errors = [], []
    for row, case in zip(holdout, cases):
        actual = float(row["target"])
        candidate_errors.append(abs(case["candidate"]["value"] - actual))
        baseline_errors.append(abs(case["baseline"]["value"] - actual))
    candidate_mae = sum(candidate_errors) / len(candidate_errors)
    baseline_mae = sum(baseline_errors) / len(baseline_errors)
    full_pairing = all(window["paired_labeled"] == window["eligible_cases"]
                       for window in report["windows"] if window["eligible_cases"])
    beats_baseline = candidate_mae < baseline_mae
    ready = len(cases) >= 5 and full_pairing and beats_baseline
    return {
        "schema": SCHEMA,
        "status": "evaluated",
        "mode": "offline_shadow_only",
        "promotion": "disabled",
        "backtest": report,
        "summary": {
            "holdout_days": len(cases),
            "candidate_mae": round(candidate_mae, 6),
            "baseline_mae": round(baseline_mae, 6),
            "beats_baseline": beats_baseline,
            "paired_label_coverage": 1.0 if full_pairing else 0.0,
        },
        "readiness": {
            "status": "ready_for_human_review" if ready else "not_ready",
            "minimum_holdout_days": 5,
            "holdout_days": len(cases),
            "all_holdout_labels_verified": full_pairing,
            "candidate_beats_training_median_baseline": beats_baseline,
            "automatic_promotion": False,
        },
        "safety": {"no_hc3_writes": True, "no_control_changes": True,
                   "fuel_forecast_unchanged": True, "return_protection": "55°C ±1°C"},
    }
