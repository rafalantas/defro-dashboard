"""Experimental, read-only fuel model.

This module deliberately has no side effects and no external dependencies. It
trains only on completed, calibrated counter days and reports a chronological
holdout score. It never changes the production fuel estimate.
"""
from __future__ import annotations

from datetime import date
from math import isfinite, sqrt
from statistics import median

MIN_TRAIN_DAYS = 45
MIN_TEST_DAYS = 7
MAX_TEST_DAYS = 14
TELEMETRY_MIN_TRAIN_DAYS = 90
TELEMETRY_MIN_TEST_DAYS = 14
MIN_RELATIVE_GAIN = 0.03
RIDGE = 0.05
HUBER_DELTA = 1.5


def _solve(matrix: list[list[float]], vector: list[float]):
    n = len(vector)
    work = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(work[row][col]))
        if abs(work[pivot][col]) < 1e-10:
            return None
        work[col], work[pivot] = work[pivot], work[col]
        divisor = work[col][col]
        work[col] = [value / divisor for value in work[col]]
        for row in range(n):
            if row == col:
                continue
            factor = work[row][col]
            if factor:
                work[row] = [a - factor * b for a, b in zip(work[row], work[col])]
    return [work[i][-1] for i in range(n)]



def _number(value) -> float | None:
    """Return only finite numeric input; reject booleans and malformed values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _valid_base_row(source: dict) -> dict | None:
    """Validate target and weather features before they enter training."""
    target = _number(source.get("kg_counter"))
    hdd_lag = _number(source.get("hdd_lag"))
    solar = _number(source.get("solar_mj"))
    wind = _number(source.get("wind_max_kmh"))
    day = source.get("day")
    if (target is None or target < 0 or hdd_lag is None or hdd_lag < 0
            or solar is None or solar < 0 or wind is None or wind < 0
            or not isinstance(day, str) or len(day) != 10):
        return None
    try:
        if date.fromisoformat(day).isoformat() != day:
            return None
    except (TypeError, ValueError):
        return None
    row = dict(source)
    row.update(kg_counter=target, hdd_lag=hdd_lag, solar_mj=solar,
               wind_max_kmh=wind)
    return row


def _valid_telemetry_row(row: dict, names: list[str]) -> bool:
    """Reject incomplete, non-finite or physically implausible telemetry days."""
    ranges = {
        "telemetry_co_c": (-10.0, 120.0),
        "telemetry_dhw_c": (0.0, 100.0),
        "telemetry_floor_c": (0.0, 85.0),
        "telemetry_return_c": (0.0, 100.0),
        "telemetry_boiler_power_kw": (0.0, 100.0),
        "telemetry_valve_opening_pct": (0.0, 100.0),
        "telemetry_pump_floor_h": (0.0, 24.0),
        "telemetry_pump_dhw_h": (0.0, 24.0),
        "telemetry_pump_circulation_h": (0.0, 24.0),
    }
    for name in names:
        value = _number(row.get(name))
        low, high = ranges[name]
        if value is None or not low <= value <= high:
            return False
    return True

def _prepare(rows: list[dict], names: list[str]):
    means, scales = [], []
    for name in names:
        values = [float(row[name]) for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        means.append(mean)
        scales.append(sqrt(variance) or 1.0)
    return means, scales


def _design(rows: list[dict], names: list[str], means: list[float], scales: list[float]):
    return [[1.0] + [(float(row[name]) - mean) / scale
                     for name, mean, scale in zip(names, means, scales)]
            for row in rows]


def _fit(rows: list[dict], names: list[str]):
    means, scales = _prepare(rows, names)
    x = _design(rows, names, means, scales)
    y = [float(row["kg_counter"]) for row in rows]
    weights = [1.0] * len(y)
    beta = None
    for _ in range(12):
        size = len(names) + 1
        matrix = [[sum(weights[k] * x[k][i] * x[k][j] for k in range(len(x)))
                   + (RIDGE if i == j and i else 0.0)
                   for j in range(size)] for i in range(size)]
        vector = [sum(weights[k] * x[k][i] * y[k] for k in range(len(x)))
                  for i in range(size)]
        beta = _solve(matrix, vector)
        if beta is None:
            return None
        residuals = [actual - sum(a * b for a, b in zip(beta, sample))
                     for actual, sample in zip(y, x)]
        center = median(residuals)
        mad = median([abs(value - center) for value in residuals])
        sigma = max(0.25, 1.4826 * mad)
        cutoff = HUBER_DELTA * sigma
        next_weights = [1.0 if abs(value) <= cutoff else cutoff / abs(value)
                        for value in residuals]
        if max(abs(a - b) for a, b in zip(weights, next_weights)) < 1e-3:
            weights = next_weights
            break
        weights = next_weights
    return {"names": names, "means": means, "scales": scales, "beta": beta}


def _predict(model: dict, row: dict) -> float:
    values = [1.0] + [(float(row[name]) - mean) / scale
                      for name, mean, scale in zip(model["names"], model["means"], model["scales"])]
    return max(0.0, sum(a * b for a, b in zip(model["beta"], values)))


def _metrics(actual: list[float], predicted: list[float]):
    errors = [prediction - target for target, prediction in zip(actual, predicted)]
    return {
        "mae_kg_day": round(sum(abs(error) for error in errors) / len(errors), 2),
        "rmse_kg_day": round(sqrt(sum(error * error for error in errors) / len(errors)), 2),
        "bias_kg_day": round(sum(errors) / len(errors), 2),
        "samples": len(errors),
    }


def _fixed_holdout_metrics(train: list[dict], test: list[dict], names: list[str]):
    """Score one untouched chronological test window using a model fitted only on train."""
    model = _fit(train, names)
    if model is None or not test:
        return None
    actual = [float(row["kg_counter"]) for row in test]
    predicted = [_predict(model, row) for row in test]
    return _metrics(actual, predicted)


def evaluate(rows: list[dict]) -> dict:
    """Compare robust models with walk-forward and fixed chronological validation.

    Walk-forward refits on an expanding history as each new actual day arrives.
    Fixed holdout fits once on the initial training window and never trains on
    any scored day. Both protocols are reported; production remains unchanged.
    """
    clean = []
    rejected_rows = 0
    rejected_by_reason = {"estimated_or_epoch": 0, "invalid_base": 0, "duplicate_day": 0}
    duplicate_days = set()
    for source in rows:
        if source.get("estimate") or source.get("counter_regime") == "epoch":
            rejected_rows += 1
            rejected_by_reason["estimated_or_epoch"] += 1
            continue
        row = _valid_base_row(source)
        if row is None:
            rejected_rows += 1
            rejected_by_reason["invalid_base"] += 1
            continue
        # Room temperature is not a model feature; invalid values are discarded.
        for room_key in ("room_c", "room_temp", "room_temperature_c"):
            value = row.get(room_key)
            room_value = _number(value)
            if value is not None and (room_value is None or room_value <= 0):
                row.pop(room_key, None)
        row["wind_hdd_lag"] = row["wind_max_kmh"] * row["hdd_lag"] / 100.0
        clean.append(row)
    day_counts = {}
    for row in clean:
        day_counts[row["day"]] = day_counts.get(row["day"], 0) + 1
    duplicate_days = {day for day, count in day_counts.items() if count > 1}
    if duplicate_days:
        rejected_by_reason["duplicate_day"] = sum(day_counts[day] for day in duplicate_days)
        rejected_rows += rejected_by_reason["duplicate_day"]
        clean = [row for row in clean if row["day"] not in duplicate_days]
    clean.sort(key=lambda row: row.get("day", ""))
    total = len(clean)
    day_gaps = []
    for previous, current in zip(clean, clean[1:]):
        gap = (date.fromisoformat(current["day"]) - date.fromisoformat(previous["day"])).days
        if gap > 1:
            day_gaps.append({"from": previous["day"], "to": current["day"], "missing_days": gap - 1})
    telemetry_names = [
        "telemetry_co_c", "telemetry_dhw_c", "telemetry_floor_c",
        "telemetry_return_c", "telemetry_boiler_power_kw",
        "telemetry_valve_opening_pct", "telemetry_pump_floor_h",
        "telemetry_pump_dhw_h", "telemetry_pump_circulation_h",
    ]
    telemetry_clean = [
        row for row in clean if _valid_telemetry_row(row, telemetry_names)
    ]
    telemetry_days = len(telemetry_clean)
    telemetry_incomplete_days = total - telemetry_days
    telemetry_test_count = min(MAX_TEST_DAYS, max(TELEMETRY_MIN_TEST_DAYS, telemetry_days // 5))
    telemetry_train_count = telemetry_days - telemetry_test_count
    telemetry_minimum_days = TELEMETRY_MIN_TRAIN_DAYS + TELEMETRY_MIN_TEST_DAYS
    test_count = min(MAX_TEST_DAYS, max(MIN_TEST_DAYS, total // 5))
    train_count = total - test_count
    if train_count < MIN_TRAIN_DAYS or test_count < MIN_TEST_DAYS:
        return {"status": "collecting", "days": total,
                "telemetry": {"status": "collecting", "complete_days": telemetry_days,
                              "incomplete_days": telemetry_incomplete_days,
                              "days_needed": telemetry_minimum_days,
                              "message": "Za mało kompletnych dób z telemetrią do walidacji"},
                "train_days_needed": MIN_TRAIN_DAYS,
                "test_days_needed": MIN_TEST_DAYS,
                "rejected_days": rejected_rows,
        "rejected_by_reason": rejected_by_reason,
        "missing_day_gaps": day_gaps,
        "telemetry_incomplete_days": telemetry_incomplete_days,
                "rejected_by_reason": rejected_by_reason,
                "missing_day_gaps": day_gaps,
                "telemetry_incomplete_days": telemetry_incomplete_days,
                "message": "Za mało kompletnych, skalibrowanych dób do uczciwego testu"}

    train, test = clean[:train_count], clean[train_count:]
    baseline_names = ["hdd_lag"]
    candidate_names = ["hdd_lag", "solar_mj", "wind_hdd_lag"]
    actual = [float(row["kg_counter"]) for row in test]
    baseline_predictions, candidate_predictions = [], []
    for index, row in enumerate(test):
        history = clean[:train_count + index]
        baseline = _fit(history, baseline_names)
        candidate = _fit(history, candidate_names)
        if baseline is None or candidate is None:
            return {"status": "unstable", "days": total,
                    "message": "Model nie przeszedł kontroli stabilności"}
        baseline_predictions.append(_predict(baseline, row))
        candidate_predictions.append(_predict(candidate, row))

    baseline_score = _metrics(actual, baseline_predictions)
    candidate_score = _metrics(actual, candidate_predictions)
    fixed_baseline_score = _fixed_holdout_metrics(train, test, baseline_names)
    fixed_candidate_score = _fixed_holdout_metrics(train, test, candidate_names)
    if fixed_baseline_score is None or fixed_candidate_score is None:
        return {"status": "unstable", "days": total,
                "message": "Model nie przeszedł kontroli stałego holdoutu"}
    gain = ((baseline_score["mae_kg_day"] - candidate_score["mae_kg_day"])
            / baseline_score["mae_kg_day"] if baseline_score["mae_kg_day"] else 0.0)
    fixed_gain = ((fixed_baseline_score["mae_kg_day"] - fixed_candidate_score["mae_kg_day"])
                  / fixed_baseline_score["mae_kg_day"]
                  if fixed_baseline_score["mae_kg_day"] else 0.0)
    accepted = gain >= MIN_RELATIVE_GAIN and fixed_gain >= MIN_RELATIVE_GAIN
    selected_names = candidate_names if accepted else baseline_names
    final_model = _fit(clean, selected_names)
    if final_model is None:
        return {"status": "unstable", "days": total,
                "message": "Nie udało się dopasować modelu końcowego"}

    telemetry_result = {"status": "collecting", "complete_days": telemetry_days,
                        "incomplete_days": telemetry_incomplete_days,
                        "days_needed": telemetry_minimum_days,
                        "training_days_needed": TELEMETRY_MIN_TRAIN_DAYS,
                        "holdout_days_needed": TELEMETRY_MIN_TEST_DAYS,
                        "message": "Model telemetrii czeka na co najmniej 90 dób treningowych i 14 testowych"}
    if telemetry_train_count >= TELEMETRY_MIN_TRAIN_DAYS and telemetry_test_count >= TELEMETRY_MIN_TEST_DAYS:
        telemetry_baseline_names = ["hdd_lag", "solar_mj", "wind_hdd_lag"]
        telemetry_candidate_names = telemetry_baseline_names + telemetry_names
        telemetry_test = telemetry_clean[telemetry_train_count:]
        telemetry_actual = [float(row["kg_counter"]) for row in telemetry_test]
        base_predictions, extended_predictions = [], []
        for index, row in enumerate(telemetry_test):
            history = telemetry_clean[:telemetry_train_count + index]
            base_fit = _fit(history, telemetry_baseline_names)
            extended_fit = _fit(history, telemetry_candidate_names)
            if base_fit is None or extended_fit is None:
                telemetry_result = {"status": "unstable", "complete_days": telemetry_days,
                                    "message": "Model telemetrii nie przeszedł kontroli stabilności"}
                break
            base_predictions.append(_predict(base_fit, row))
            extended_predictions.append(_predict(extended_fit, row))
        else:
            base_score = _metrics(telemetry_actual, base_predictions)
            extended_score = _metrics(telemetry_actual, extended_predictions)
            telemetry_fixed_base = _fixed_holdout_metrics(
                telemetry_clean[:telemetry_train_count], telemetry_test, telemetry_baseline_names)
            telemetry_fixed_extended = _fixed_holdout_metrics(
                telemetry_clean[:telemetry_train_count], telemetry_test, telemetry_candidate_names)
            if telemetry_fixed_base is None or telemetry_fixed_extended is None:
                telemetry_result = {"status": "unstable", "complete_days": telemetry_days,
                                    "message": "Stały holdout telemetrii nie przeszedł kontroli stabilności"}
            else:
                telemetry_fixed_gain = (
                    (telemetry_fixed_base["mae_kg_day"] - telemetry_fixed_extended["mae_kg_day"])
                    / telemetry_fixed_base["mae_kg_day"]
                    if telemetry_fixed_base["mae_kg_day"] else 0.0
                )
                telemetry_gain = ((base_score["mae_kg_day"] - extended_score["mae_kg_day"])
                                  / base_score["mae_kg_day"] if base_score["mae_kg_day"] else 0.0)
                telemetry_accepted = (
                    telemetry_gain >= MIN_RELATIVE_GAIN
                    and telemetry_fixed_gain >= MIN_RELATIVE_GAIN
                )
                telemetry_result = {
                    "status": "shadow_ready", "mode": "shadow_only",
                    "complete_days": telemetry_days, "training_days": telemetry_train_count,
                    "holdout_days": telemetry_test_count,
                    "holdout_from": telemetry_test[0].get("day"),
                    "holdout_to": telemetry_test[-1].get("day"),
                    "baseline": base_score, "candidate": extended_score,
                    "validation": {
                        "walk_forward": {"baseline": base_score, "candidate": extended_score},
                        "fixed_holdout": {"baseline": telemetry_fixed_base,
                                          "candidate": telemetry_fixed_extended},
                    },
                    "mae_gain_percent": round(telemetry_gain * 100, 1),
                    "fixed_holdout_mae_gain_percent": round(telemetry_fixed_gain * 100, 1),
                    "accepted": telemetry_accepted,
                    "selected": "telemetry_enhanced" if telemetry_accepted else "weather_only",
                    "features": telemetry_candidate_names if telemetry_accepted else telemetry_baseline_names,
                    "safety": "shadow_only; production estimates are unchanged",
                    "validation_note": "walk-forward and fixed holdout currently score the same terminal dates; treat as complementary protocols, not independent samples",
                }

    return {
        "status": "shadow_ready",
        "mode": "shadow_only",
        "telemetry": telemetry_result,
        "training_days": total,
        "holdout_days": test_count,
        "trained_through": clean[-1].get("day"),
        "features": selected_names,
        "selected": "weather_aware" if accepted else "temperature_only",
        "accepted_weather_gain": accepted,
        "fixed_holdout_mae_gain_percent": round(fixed_gain * 100, 1),
        "rejected_days": rejected_rows,
        "mae_gain_percent": round(gain * 100, 1),
        "baseline": baseline_score,
        "candidate": candidate_score,
        "validation": {
            "protocol": "walk_forward_expanding_window",
            "independence_note": "walk-forward and fixed holdout score the same terminal dates; metrics are complementary, not independent evidence",
            "walk_forward": {"baseline": baseline_score, "candidate": candidate_score},
            "fixed_holdout": {
                "training_from": train[0].get("day"),
                "training_to": train[-1].get("day"),
                "scored_from": test[0].get("day"),
                "scored_to": test[-1].get("day"),
                "baseline": fixed_baseline_score,
                "candidate": fixed_candidate_score,
            },
        },
        "holdout_from": test[0].get("day"),
        "holdout_to": test[-1].get("day"),
        "model": {
            "intercept": round(final_model["beta"][0], 4),
            "coefficients_standardized": {
                name: round(value, 4)
                for name, value in zip(selected_names, final_model["beta"][1:])
            },
            "feature_means": dict(zip(selected_names, [round(value, 4) for value in final_model["means"]])),
            "feature_scales": dict(zip(selected_names, [round(value, 4) for value in final_model["scales"]])),
        },
        "safety": "shadow_only; production estimates are unchanged",
    }
