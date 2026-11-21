"""Paired offline ablation of as-issued weather forecasts for fuel periods."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from thermoflow_fuel_ml import FEATURE_KEYS, TARGET_KEY, _instant, _scores, prepare_fuel_dataset
from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import predict, train_and_evaluate

WEATHER = "forecast_mean_outdoor_c"


def evaluate_weather_impact(snapshot):
    fuel = prepare_fuel_dataset(snapshot, latest_regime_only=True)["dataset"]
    result = {"schema": "thermoflow-weather-ablation/1", "promotion": "disabled",
              "status": "collecting", "reason": "insufficient_paired_periods",
              "fuel_eligible": fuel["eligible_samples"], "paired": 0, "evaluation": None}
    weather = defaultdict(list)
    forecasts = snapshot.get("forecasts", [])
    if not isinstance(forecasts, list):
        raise ValueError("forecasts must be a list")
    ends = {}
    for period in snapshot["periods"]:
        try:
            ends[_instant(period["start"])] = _instant(period["end"])
        except (TypeError, KeyError, ValueError, OverflowError, OSError):
            continue
    for forecast in forecasts:
        try:
            start = _instant(forecast["start"])
            issue = _instant(forecast["issued_at"])
            received = _instant(forecast["available_at"])
            value = forecast[WEATHER]
            if (forecast["kind"] != "forecast" or not issue <= received <= start
                    or _instant(forecast["end"]) != ends.get(start)
                    or type(value) not in (int, float) or not -90 <= value <= 65):
                continue
            weather[start].append((received, issue, value))
        except (TypeError, KeyError, ValueError, OverflowError, OSError):
            continue
    rows = []
    for row in fuel["training"] + fuel["holdout"]:
        options = weather.get(row["timestamp"])
        if not options:
            continue
        latest = max(option[:2] for option in options)
        values = {option[2] for option in options if option[:2] == latest}
        if len(values) != 1:
            continue
        rows.append({"timestamp": row["timestamp"], "complete": True,
                     TARGET_KEY: row["target"], **row["features"], WEATHER: values.pop()})
    result["paired"] = len(rows)
    if len(rows) < 25:
        return result
    scores = {}
    for name, keys in (("without_weather", FEATURE_KEYS), ("with_weather", (*FEATURE_KEYS, WEATHER))):
        dataset = prepare_chronological_dataset(rows, feature_keys=keys, target_key=TARGET_KEY)
        try:
            trained = train_and_evaluate(dataset)
        except ValueError as error:
            if str(error) != "features are linearly dependent; model is not identifiable":
                raise
            return {**result, "status": "unavailable", "reason": f"degenerate_{name}"}
        holdout = dataset["holdout"]
        values = [predict(trained["model"], row["features"])["prediction"] for row in holdout]
        if any(value <= 0 for value in values):
            return {**result, "status": "unavailable", "reason": f"nonphysical_{name}"}
        scores[name] = _scores([row["target"] for row in holdout], values)
    return {**result, "status": "evaluated", "reason": None, "evaluation": scores,
            "holdout_mae_gain_kg_per_day": (scores["without_weather"]["mae_kg_per_day"]
                                            - scores["with_weather"]["mae_kg_per_day"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate_weather_impact(json.loads(args.snapshot.read_text())), allow_nan=False))
