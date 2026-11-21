"""Offline fuel-rate benchmark from verified refill intervals."""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections import Counter
from datetime import datetime
from math import isfinite, sqrt
from pathlib import Path

from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import predict, train_and_evaluate

INPUT_SCHEMA = "thermoflow-verified-fuel-periods/1"
REPORT_SCHEMA = "thermoflow-fuel-shadow/1"
FEATURE_KEYS = ("baseline_kg_per_day", "previous_kg_per_day")
TARGET_KEY = "observed_kg_per_day"


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("expected an ISO timestamp with offset")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must have an offset")
    return parsed.timestamp()


def _positive(value):
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise ValueError("expected positive finite mass or rate")
    return float(value)


def _intervals(sources, rejected):
    events = []
    for source in sources:
        try:
            start, end = _instant(source["start"]), _instant(source["end"])
            if not 0 < start < end:
                raise ValueError
            events.append((start, end, source))
        except (TypeError, KeyError, ValueError, OverflowError, OSError):
            rejected["invalid_interval"] += 1
    # Reject whole overlapping groups, including exact duplicates, before
    # checking labels; a bad record cannot let an overlapping record sneak in.
    group = []
    right = None
    for event in sorted(events, key=lambda event: event[:2]):
        if group and event[0] >= right:
            if len(group) == 1:
                yield group[0]
            else:
                rejected["overlapping_or_duplicate_interval"] += len(group)
            group, right = [], None
        group.append(event)
        right = event[1] if right is None else max(right, event[1])
    if len(group) == 1:
        yield group[0]
    elif group:
        rejected["overlapping_or_duplicate_interval"] += len(group)


def prepare_fuel_dataset(snapshot, *, latest_regime_only=False):
    """Build rates without treating arbitrary refills as fuel consumption."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"expected {INPUT_SCHEMA} input")
    sources = snapshot.get("periods")
    epochs_raw = snapshot.get("calibration_epochs")
    if not isinstance(sources, list) or not isinstance(epochs_raw, list):
        raise ValueError("periods and calibration_epochs must be lists")
    epochs = sorted({_instant(value) for value in epochs_raw})
    previous = None
    rows = []
    rejected = Counter()
    for start, end, source in _intervals(sources, rejected):
        regime = bisect_right(epochs, start)
        if regime < len(epochs) and epochs[regime] < end:
            rejected["crosses_calibration_epoch"] += 1
            previous = None
            continue
        if latest_regime_only and regime != len(epochs):
            rejected["prior_calibration_epoch"] += 1
            continue
        if source.get("mass_reference") != "verified_equal_fill_level":
            rejected["unverified_mass_balance"] += 1
            previous = None
            continue
        try:
            actual = _positive(_positive(source["refilled_kg"]) / ((end - start) / 86400))
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["invalid_mass"] += 1
            previous = None
            continue

        # The most recent measured rate is available once its interval closes.
        prev = previous
        previous = end, regime, actual
        try:
            baseline = _positive(source["baseline_kg_per_day"])
            if _instant(source["baseline_issued_at"]) > start:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            rejected["missing_invalid_or_late_baseline"] += 1
            continue
        if prev is None or prev[:2] != (start, regime):
            rejected["no_adjacent_verified_period_in_regime"] += 1
            continue
        rows.append({"timestamp": start, "complete": True, FEATURE_KEYS[0]: baseline,
                     FEATURE_KEYS[1]: prev[2], TARGET_KEY: actual})

    return {
        "source_periods": len(sources),
        "excluded_periods_by_reason": dict(sorted(rejected.items())),
        "dataset": prepare_chronological_dataset(rows, feature_keys=FEATURE_KEYS,
                                                 target_key=TARGET_KEY),
    }


def _scores(actual, predicted):
    errors = [guess - target for target, guess in zip(actual, predicted, strict=True)]
    return {
        "samples": len(errors),
        "mae_kg_per_day": sum(abs(error) for error in errors) / len(errors),
        "rmse_kg_per_day": sqrt(sum(error * error for error in errors) / len(errors)),
        "bias_kg_per_day": sum(errors) / len(errors),
    }


def evaluate_fuel_model(snapshot):
    prepared = prepare_fuel_dataset(snapshot, latest_regime_only=True)
    dataset = prepared["dataset"]
    result = {
        "schema": REPORT_SCHEMA, "mode": "offline_shadow_only", "promotion": "disabled",
        "model": None, "evaluation": None, **prepared,
    }
    try:
        trained = train_and_evaluate(dataset)
    except ValueError as error:
        if str(error) != "features are linearly dependent; model is not identifiable":
            raise
        trained = {"status": "unavailable", "reason": "degenerate_training_features", "model": None}
    if trained["status"] == "collecting":
        return {**result, "status": "collecting", "reason": trained["reason"]}

    holdout = dataset["holdout"]
    actual = [row["target"] for row in holdout]
    baseline = _scores(actual, [row["features"][FEATURE_KEYS[0]] for row in holdout])
    previous = _scores(actual, [row["features"][FEATURE_KEYS[1]] for row in holdout])
    result["evaluation"] = {"as_issued_baseline": baseline, "previous_period": previous,
                            "candidate": None}
    if trained["model"] is None:
        return {**result, "status": "unavailable", "reason": trained["reason"]}
    values = [predict(trained["model"], row["features"])["prediction"] for row in holdout]
    if any(not isfinite(value) or value <= 0 for value in values):
        return {**result, "status": "unavailable", "reason": "nonphysical_holdout_prediction"}
    candidate = _scores(actual, values)
    result["evaluation"]["candidate"] = {
        **candidate, "beats_as_issued_baseline_on_mae":
            candidate["mae_kg_per_day"] < baseline["mae_kg_per_day"],
    }
    return {**result, "status": "evaluated", "model": trained["model"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("periods", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate_fuel_model(json.loads(args.periods.read_text(encoding="utf-8")))
        output = json.dumps(result, allow_nan=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f"Fuel benchmark failed: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
