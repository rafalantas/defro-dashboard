"""Experimental, read-only fuel model.

This module deliberately has no side effects and no external dependencies. It
trains only on completed, calibrated counter days and reports a chronological
holdout score. It never changes the production fuel estimate.
"""
from __future__ import annotations

from math import sqrt
from statistics import median

MIN_TRAIN_DAYS = 45
MIN_TEST_DAYS = 7
MAX_TEST_DAYS = 14
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


def evaluate(rows: list[dict]) -> dict:
    """Fit a robust weather-aware model and compare it to temperature-only baseline.

    The final chronological holdout is never used to fit the scored models.
    """
    clean = []
    for source in rows:
        if (source.get("kg_counter") is None or source.get("estimate")
                or source.get("counter_regime") == "epoch"
                or source.get("hdd_lag") is None
                or source.get("solar_mj") is None
                or source.get("wind_max_kmh") is None):
            continue
        row = dict(source)
        # Temperatura pokoju nie jest obecnie cechą modelu paliwa. Jeśli
        # zostanie przekazana, odczyty 0°C lub niepoprawne są usuwane,
        # nigdy imputowane ani wykorzystywane w żadnej kalkulacji.
        for room_key in ("room_c", "room_temp", "room_temperature_c"):
            value = row.get(room_key)
            if value is not None:
                try:
                    room_value = float(value)
                except (TypeError, ValueError):
                    row.pop(room_key, None)
                else:
                    if not (room_value > 0 and room_value < float("inf")):
                        row.pop(room_key, None)
        row["wind_hdd_lag"] = float(row["wind_max_kmh"]) * float(row["hdd_lag"]) / 100.0
        clean.append(row)
    clean.sort(key=lambda row: row.get("day", ""))
    total = len(clean)
    telemetry_names = [
        "telemetry_co_c", "telemetry_dhw_c", "telemetry_floor_c",
        "telemetry_return_c", "telemetry_boiler_power_kw",
        "telemetry_valve_opening_pct", "telemetry_pump_floor_h",
        "telemetry_pump_dhw_h", "telemetry_pump_circulation_h",
    ]
    telemetry_clean = [
        row for row in clean
        if all(row.get(name) is not None for name in telemetry_names)
    ]
    telemetry_days = len(telemetry_clean)
    test_count = min(MAX_TEST_DAYS, max(MIN_TEST_DAYS, total // 5))
    train_count = total - test_count
    if train_count < MIN_TRAIN_DAYS or test_count < MIN_TEST_DAYS:
        return {"status": "collecting", "days": total,
                "telemetry": {"status": "collecting", "complete_days": telemetry_days,
                              "days_needed": MIN_TRAIN_DAYS + MIN_TEST_DAYS,
                              "message": "Za mało kompletnych dób z telemetrią do walidacji"},
                "train_days_needed": MIN_TRAIN_DAYS,
                "test_days_needed": MIN_TEST_DAYS,
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
    gain = ((baseline_score["mae_kg_day"] - candidate_score["mae_kg_day"])
            / baseline_score["mae_kg_day"] if baseline_score["mae_kg_day"] else 0.0)
    accepted = gain >= MIN_RELATIVE_GAIN
    selected_names = candidate_names if accepted else baseline_names
    final_model = _fit(clean, selected_names)
    if final_model is None:
        return {"status": "unstable", "days": total,
                "message": "Nie udało się dopasować modelu końcowego"}

    telemetry_result = {"status": "collecting", "complete_days": telemetry_days,
                        "days_needed": MIN_TRAIN_DAYS + MIN_TEST_DAYS,
                        "message": "Model telemetrii czeka na co najmniej 45 dób treningowych i 7 testowych"}
    if telemetry_days >= MIN_TRAIN_DAYS + MIN_TEST_DAYS:
        telemetry_test_count = min(MAX_TEST_DAYS, max(MIN_TEST_DAYS, telemetry_days // 5))
        telemetry_train_count = telemetry_days - telemetry_test_count
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
            telemetry_gain = ((base_score["mae_kg_day"] - extended_score["mae_kg_day"])
                              / base_score["mae_kg_day"] if base_score["mae_kg_day"] else 0.0)
            telemetry_result = {
                "status": "shadow_ready", "mode": "shadow_only",
                "complete_days": telemetry_days, "training_days": telemetry_train_count,
                "holdout_days": telemetry_test_count,
                "holdout_from": telemetry_test[0].get("day"),
                "holdout_to": telemetry_test[-1].get("day"),
                "baseline": base_score, "candidate": extended_score,
                "mae_gain_percent": round(telemetry_gain * 100, 1),
                "accepted": telemetry_gain >= MIN_RELATIVE_GAIN,
                "features": telemetry_candidate_names if telemetry_gain >= MIN_RELATIVE_GAIN else telemetry_baseline_names,
                "safety": "shadow_only; production estimates are unchanged",
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
        "mae_gain_percent": round(gain * 100, 1),
        "baseline": baseline_score,
        "candidate": candidate_score,
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
