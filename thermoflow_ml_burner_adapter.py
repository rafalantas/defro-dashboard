"""Build supervised ML rows from validated ThermoFlow burner-cycle diagnostics."""
from __future__ import annotations

from datetime import datetime
from math import isfinite

from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import train_and_evaluate

FEATURE_KEYS = ("previous_duration_minutes", "idle_minutes")
TARGET_KEY = "duration_minutes"


def _timestamp(value) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("cycle timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    timestamp = parsed.timestamp()
    if not isfinite(timestamp) or timestamp <= 0:
        raise ValueError("invalid cycle timestamp")
    return timestamp


def prepare_burner_cycle_dataset(burner_cycles: dict) -> dict:
    """Convert consecutive complete FIRE-confirmed cycles into forecast samples.

    At cycle start, the only predictors are the previous completed burn duration
    and the observed idle interval since that cycle ended. The current duration
    is the target. Any intervening incomplete/censored cycle breaks the sequence.
    """
    if not isinstance(burner_cycles, dict):
        raise ValueError("burner_cycles must be a mapping")
    complete = burner_cycles.get("cycles") or []
    incomplete = burner_cycles.get("incomplete_cycles") or []
    events = []
    rejected = {"malformed_cycle": 0, "non_positive_duration": 0, "overlapping_cycles": 0}

    for cycle in complete:
        if not isinstance(cycle, dict) or cycle.get("complete") is not True:
            rejected["malformed_cycle"] += 1
            continue
        try:
            start = _timestamp(cycle["start"])
            end = _timestamp(cycle["end"])
            duration = float(cycle["duration_minutes"])
            if not isfinite(duration) or duration <= 0 or end <= start:
                raise ValueError("invalid duration")
            events.append({"start": start, "end": end, "duration": duration, "complete": True})
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["malformed_cycle"] += 1

    for cycle in incomplete:
        if not isinstance(cycle, dict):
            rejected["malformed_cycle"] += 1
            continue
        try:
            start = _timestamp(cycle["start"])
            end = _timestamp(cycle["end"])
            if end < start:
                raise ValueError("invalid interval")
            events.append({"start": start, "end": end, "duration": None, "complete": False})
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["malformed_cycle"] += 1

    events.sort(key=lambda item: (item["start"], item["end"]))
    samples = []
    previous = None
    for event in events:
        if previous is not None and event["start"] < previous["end"]:
            rejected["overlapping_cycles"] += 1
            previous = event
            continue
        if event["complete"] and previous is not None and previous["complete"]:
            idle_minutes = (event["start"] - previous["end"]) / 60.0
            if idle_minutes >= 0 and previous["duration"] > 0:
                samples.append({
                    "timestamp": event["start"],
                    "complete": True,
                    "previous_duration_minutes": previous["duration"],
                    "idle_minutes": idle_minutes,
                    TARGET_KEY: event["duration"],
                })
        previous = event

    dataset = prepare_chronological_dataset(
        samples,
        feature_keys=FEATURE_KEYS,
        target_key=TARGET_KEY,
    )
    dataset["source"] = "thermoflow_fire_confirmed_burner_cycles"
    dataset["source_cycles"] = len(complete)
    dataset["usable_forecast_samples"] = len(samples)
    dataset["adapter_rejected_cycles"] = rejected
    dataset["feature_semantics"] = {
        "previous_duration_minutes": "duration of the immediately preceding complete FIRE-confirmed burner cycle",
        "idle_minutes": "elapsed time from preceding complete cycle end to current cycle start",
    }
    dataset["target_semantics"] = "current complete FIRE-confirmed burner cycle duration"
    dataset["safety"] = "derived_read_only_from_diagnostics_no_ignition_inference_no_control_effect"
    return dataset


def train_burner_cycle_model(burner_cycles: dict) -> dict:
    """Prepare and evaluate a shadow model; no artifact is persisted or promoted."""
    dataset = prepare_burner_cycle_dataset(burner_cycles)
    result = train_and_evaluate(dataset)
    result["dataset"] = dataset
    result["model_purpose"] = "experimental_burner_cycle_duration_estimate"
    result["promotion"] = "disabled_shadow_only"
    return result
