"""Experimental, read-only fuel model.

This module deliberately has no side effects and no external dependencies. It
trains on completed refill-period observations (or legacy counter days) and reports a chronological
holdout score. It never changes the production fuel estimate.
"""
from __future__ import annotations

from datetime import date
from math import isfinite, sqrt
from statistics import median

MIN_TRAIN_PERIODS = 45
MIN_TEST_PERIODS = 7
MAX_TEST_PERIODS = 14
# Backward-compatible aliases for clients/tests that still use the old names.
MIN_TRAIN_DAYS = MIN_TRAIN_PERIODS
MIN_TEST_DAYS = MIN_TEST_PERIODS
MAX_TEST_DAYS = MAX_TEST_PERIODS
TELEMETRY_MIN_TRAIN_PERIODS = 20
TELEMETRY_MIN_TEST_PERIODS = 5
# Backward-compatible constant aliases; values now count refill-period samples.
TELEMETRY_MIN_TRAIN_DAYS = TELEMETRY_MIN_TRAIN_PERIODS
TELEMETRY_MIN_TEST_DAYS = TELEMETRY_MIN_TEST_PERIODS
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


def _base_row_rejection_reason(source: dict) -> str | None:
    """Return the first failed required base field without weakening validation."""
    checks = (
        ("kg_counter", "kg_counter_missing_or_invalid", "kg_counter_negative"),
        ("hdd_lag", "hdd_lag_missing_or_invalid", "hdd_lag_negative"),
        ("solar_mj", "solar_mj_missing_or_invalid", "solar_mj_negative"),
        ("wind_max_kmh", "wind_max_kmh_missing_or_invalid", "wind_max_kmh_negative"),
    )
    for field, invalid_reason, negative_reason in checks:
        value = _number(source.get(field))
        if value is None:
            return invalid_reason
        if value < 0:
            return negative_reason
    day = source.get("day")
    if not isinstance(day, str) or len(day) != 10:
        return "day_missing_or_invalid"
    try:
        if date.fromisoformat(day).isoformat() != day:
            return "day_missing_or_invalid"
    except (TypeError, ValueError):
        return "day_missing_or_invalid"
    return None


def _valid_base_row(source: dict) -> dict | None:
    """Validate target and weather features before they enter training."""
    if _base_row_rejection_reason(source) is not None:
        return None
    row = dict(source)
    row.update(
        kg_counter=_number(source.get("kg_counter")),
        hdd_lag=_number(source.get("hdd_lag")),
        solar_mj=_number(source.get("solar_mj")),
        wind_max_kmh=_number(source.get("wind_max_kmh")),
    )
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


def _select_telemetry_features(rows: list[dict], names: list[str], minimum_periods: int):
    """Choose the broadest feature set with enough fully observed periods.

    Selection uses telemetry availability only, never target values. Missing
    values are not imputed; each selected feature is valid in every retained
    period.
    """
    coverage = {
        name: sum(_valid_telemetry_row(row, [name]) for row in rows)
        for name in names
    }
    ordered = sorted(names, key=lambda name: (-coverage[name], names.index(name)))
    selected = []
    eligible = list(rows)
    for name in ordered:
        candidate = [row for row in eligible
                     if _valid_telemetry_row(row, selected + [name])]
        if len(candidate) >= minimum_periods:
            selected.append(name)
            eligible = candidate
    return selected, eligible, coverage


def _period_diagnostic(row: dict, status: str, reason_codes=None, telemetry_names=None) -> dict:
    """Explain model eligibility without pretending aggregated features are raw samples."""
    day = row.get("day")
    end = row.get("end")
    start = row.get("start")
    date_value = day or (str(end)[:10] if end else None) or (str(start)[:10] if start else None)
    result = {
        "date": date_value,
        "period_start": start,
        "period_end": end,
        "status": status,
        "reason_codes": list(reason_codes or []),
        "missing_fields": [],
        "invalid_fields": [],
        "telemetry_feature_count": None,
        "telemetry_feature_total": len(telemetry_names) if telemetry_names else None,
        "telemetry_coverage_percent": None,
        # Refill-period rows contain aggregated features, not raw timestamped samples.
        "samples_expected": None,
        "samples_received": None,
        "max_gap_minutes": None,
        "diagnostic_granularity": "aggregated_period_features",
    }
    if telemetry_names is None:
        return result

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
    valid_count = 0
    for name in telemetry_names:
        raw = row.get(name)
        value = _number(raw)
        if value is None:
            if raw is None:
                result["missing_fields"].append(name)
                result["reason_codes"].append(f"telemetry_missing:{name}")
            else:
                result["invalid_fields"].append(name)
                result["reason_codes"].append(f"telemetry_invalid:{name}")
            continue
        low, high = ranges[name]
        if not low <= value <= high:
            result["invalid_fields"].append(name)
            result["reason_codes"].append(f"telemetry_out_of_range:{name}")
            continue
        valid_count += 1

    result["telemetry_feature_count"] = valid_count
    result["telemetry_coverage_percent"] = round(
        valid_count / len(telemetry_names) * 100, 1
    ) if telemetry_names else None
    result["status"] = "complete" if not result["reason_codes"] else "incomplete"
    return result


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



def _coefficient_stability(rows: list[dict], names: list[str]) -> dict:
    """Describe standardized coefficient drift across expanding training windows."""
    count = len(rows)
    window_sizes = sorted({
        max(MIN_TRAIN_PERIODS, int(count * 0.6 + 0.999999)),
        max(MIN_TRAIN_PERIODS, int(count * 0.8 + 0.999999)),
        count,
    })
    if len(window_sizes) < 3 or window_sizes[0] > count:
        return {
            "available": False,
            "reason": "insufficient_history_for_three_expanding_windows",
            "windows": [],
            "features": {},
        }

    snapshots = []
    for size in window_sizes:
        model = _fit(rows[:size], names)
        if model is None:
            return {
                "available": False,
                "reason": "one_or_more_expanding_window_fits_unstable",
                "windows": snapshots,
                "features": {},
            }
        snapshots.append({
            "training_periods": size,
            "training_to": rows[size - 1].get("day"),
            "standardized_coefficients": {
                name: round(float(coefficient), 6)
                for name, coefficient in zip(names, model["beta"][1:])
            },
        })

    features = {}
    for name in names:
        values = [snapshot["standardized_coefficients"][name] for snapshot in snapshots]
        signs = [1 if value > 0 else (-1 if value < 0 else 0) for value in values]
        nonzero_signs = [sign for sign in signs if sign]
        features[name] = {
            "values": values,
            "min": round(min(values), 6),
            "max": round(max(values), 6),
            "sign_consistent": bool(nonzero_signs)
                and len(nonzero_signs) == len(signs)
                and len(set(nonzero_signs)) == 1,
        }
    return {
        "available": True,
        "method": "expanding chronological training windows; standardized coefficients; descriptive only",
        "windows": snapshots,
        "features": features,
    }



def evaluate(rows: list[dict]) -> dict:
    """Compare robust models with walk-forward and fixed chronological validation.

    Walk-forward refits on an expanding history as each new actual refill period arrives.
    Fixed holdout fits once on the initial training window and never trains on
    any scored period. Both protocols are reported; production remains unchanged.
    """
    clean = []
    rejected_rows = 0
    rejected_by_reason = {"estimated_or_epoch": 0, "invalid_base": 0, "duplicate_day": 0}
    invalid_base_by_reason = {}
    counter_day_diagnostics = []
    period_diagnostics = []
    duplicate_days = set()
    for source in rows:
        if source.get("estimate") or source.get("counter_regime") == "epoch":
            rejected_rows += 1
            rejected_by_reason["estimated_or_epoch"] += 1
            reasons = []
            if source.get("estimate"):
                reasons.append("estimated_row")
            if source.get("counter_regime") == "epoch":
                reasons.append("counter_epoch")
            period_diagnostics.append(_period_diagnostic(source, "rejected", reasons))
            continue
        base_rejection = _base_row_rejection_reason(source)
        row = _valid_base_row(source)
        if row is None:
            rejected_rows += 1
            rejected_by_reason["invalid_base"] += 1
            invalid_base_by_reason[base_rejection] = invalid_base_by_reason.get(base_rejection, 0) + 1
            period_diagnostics.append(_period_diagnostic(source, "rejected", [base_rejection]))
            if base_rejection == "kg_counter_missing_or_invalid" and source.get("counter_diagnostics"):
                counter_day_diagnostics.append(source["counter_diagnostics"])
            continue
        # Room temperature is not a model feature; invalid values are discarded.
        for room_key in ("room_c", "room_temp", "room_temperature_c"):
            value = row.get(room_key)
            room_value = _number(value)
            if value is not None and (room_value is None or room_value <= 0):
                row.pop(room_key, None)
        row["wind_hdd_lag"] = row["wind_max_kmh"] * row["hdd_lag"] / 100.0
        clean.append(row)
    # Daily observations must be unique by calendar day. Refill-period
    # observations are event intervals: multiple legitimate refills can close
    # on the same date, so their exact end timestamp is the sample identity.
    daily_counts = {}
    period_counts = {}
    for row in clean:
        if row.get("source") == "refill_period":
            identity = row.get("end")
            period_counts[identity] = period_counts.get(identity, 0) + 1
        else:
            daily_counts[row["day"]] = daily_counts.get(row["day"], 0) + 1
    duplicate_days = {day for day, count in daily_counts.items() if count > 1}
    duplicate_periods = {stamp for stamp, count in period_counts.items() if count > 1}
    if duplicate_days or duplicate_periods:
        for row in clean:
            is_duplicate = (
                (row.get("source") == "refill_period" and row.get("end") in duplicate_periods)
                or (row.get("source") != "refill_period" and row.get("day") in duplicate_days)
            )
            if is_duplicate:
                period_diagnostics.append(_period_diagnostic(row, "rejected", ["duplicate_day"]))
        rejected_by_reason["duplicate_day"] = (
            sum(daily_counts[day] for day in duplicate_days)
            + sum(period_counts[stamp] for stamp in duplicate_periods)
        )
        rejected_rows += rejected_by_reason["duplicate_day"]
        clean = [
            row for row in clean
            if (row.get("source") == "refill_period"
                and row.get("end") not in duplicate_periods)
            or (row.get("source") != "refill_period"
                and row["day"] not in duplicate_days)
        ]
    clean.sort(key=lambda row: (row.get("day", ""), row.get("end", "")))
    total = len(clean)
    telemetry_names = [
        "telemetry_co_c", "telemetry_dhw_c", "telemetry_floor_c",
        "telemetry_return_c", "telemetry_boiler_power_kw",
        "telemetry_valve_opening_pct", "telemetry_pump_floor_h",
        "telemetry_pump_dhw_h", "telemetry_pump_circulation_h",
    ]
    telemetry_selected_features, telemetry_clean, telemetry_feature_coverage = (
        _select_telemetry_features(
            clean, telemetry_names,
            TELEMETRY_MIN_TRAIN_PERIODS + TELEMETRY_MIN_TEST_PERIODS,
        )
    )
    period_diagnostics.extend(
        _period_diagnostic(row, "complete" if row in telemetry_clean else "incomplete",
                           telemetry_names=telemetry_names)
        for row in clean
    )
    period_diagnostics.sort(
        key=lambda item: (item.get("date") or "", item.get("period_end") or "")
    )
    telemetry_periods = len(telemetry_clean)
    telemetry_incomplete_periods = total - telemetry_periods
    telemetry_test_count = TELEMETRY_MIN_TEST_PERIODS
    telemetry_train_count = telemetry_periods - telemetry_test_count
    telemetry_minimum_periods = TELEMETRY_MIN_TRAIN_PERIODS + TELEMETRY_MIN_TEST_PERIODS
    # Keep the old names as compatibility aliases for clients deployed before this API change.
    telemetry_days = telemetry_periods
    telemetry_incomplete_days = telemetry_incomplete_periods
    telemetry_minimum_days = telemetry_minimum_periods
    test_count = min(MAX_TEST_DAYS, max(MIN_TEST_DAYS, total // 5))
    train_count = total - test_count
    if train_count < MIN_TRAIN_PERIODS or test_count < MIN_TEST_PERIODS:
        return {"period_diagnostics": period_diagnostics, "status": "collecting", "days": total, "periods": total,
                "telemetry": {"status": "collecting", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                              "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                              "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                              "message": "Za mało kompletnych okresów z telemetrią do walidacji"},
                "train_days_needed": MIN_TRAIN_DAYS, "train_periods_needed": MIN_TRAIN_PERIODS,
                "test_days_needed": MIN_TEST_DAYS, "test_periods_needed": MIN_TEST_PERIODS,
                "required_periods": MIN_TRAIN_PERIODS + MIN_TEST_PERIODS,
                "available_periods": total,
                "rejected_days": rejected_rows, "rejected_periods": rejected_rows,
        "rejected_by_reason": rejected_by_reason,
        "invalid_base_by_reason": invalid_base_by_reason,
        "counter_day_diagnostics": counter_day_diagnostics,
        "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods,
        "telemetry_incomplete_periods": telemetry_incomplete_periods, "telemetry_incomplete_periods": telemetry_incomplete_periods,
                "rejected_by_reason": rejected_by_reason,
                        "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods, "telemetry_incomplete_periods": telemetry_incomplete_periods,
                "message": "Za mało kompletnych okresów dosypka–dosypka do uczciwego testu"}

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
            return {"period_diagnostics": period_diagnostics, "status": "unstable", "days": total, "periods": total,
                    "telemetry": {"status": "not_evaluated", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                                  "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                                  "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                                  "message": "Model bazowy niestabilny; model telemetrii nie został oceniony"},
                    "rejected_days": rejected_rows, "rejected_periods": rejected_rows, "rejected_by_reason": rejected_by_reason,
                    "invalid_base_by_reason": invalid_base_by_reason,
        "counter_day_diagnostics": counter_day_diagnostics,
                    "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods, "telemetry_incomplete_periods": telemetry_incomplete_periods,
                    "message": "Model nie przeszedł kontroli stabilności"}
        baseline_predictions.append(_predict(baseline, row))
        candidate_predictions.append(_predict(candidate, row))

    baseline_score = _metrics(actual, baseline_predictions)
    candidate_score = _metrics(actual, candidate_predictions)
    period_comparisons = []
    for row, actual_rate, baseline_rate, candidate_rate in zip(
            test, actual, baseline_predictions, candidate_predictions):
        duration = _number(row.get("period_days"))
        period_comparisons.append({
            "period_start": row.get("start"),
            "period_end": row.get("end"),
            "period_days": duration,
            "actual_kg_day": round(actual_rate, 3),
            "baseline_predicted_kg_day": round(baseline_rate, 3),
            "candidate_predicted_kg_day": round(candidate_rate, 3),
            "baseline_error_kg_day": round(baseline_rate - actual_rate, 3),
            "candidate_error_kg_day": round(candidate_rate - actual_rate, 3),
            "actual_period_kg": round(actual_rate * duration, 2) if duration else None,
            "baseline_predicted_period_kg": round(baseline_rate * duration, 2) if duration else None,
            "candidate_predicted_period_kg": round(candidate_rate * duration, 2) if duration else None,
        })
    fixed_baseline_score = _fixed_holdout_metrics(train, test, baseline_names)
    fixed_candidate_score = _fixed_holdout_metrics(train, test, candidate_names)
    ablation_specs = {
        "hdd_only": baseline_names,
        "hdd_plus_solar": ["hdd_lag", "solar_mj"],
        "hdd_solar_wind_interaction": candidate_names,
    }
    ablation_scores = {
        label: _fixed_holdout_metrics(train, test, names)
        for label, names in ablation_specs.items()
    }
    ablation_available = all(score is not None for score in ablation_scores.values())
    if fixed_baseline_score is None or fixed_candidate_score is None:
        return {"period_diagnostics": period_diagnostics, "status": "unstable", "days": total, "periods": total,
                "telemetry": {"status": "not_evaluated", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                              "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                              "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                              "message": "Model bazowy niestabilny; model telemetrii nie został oceniony"},
                "rejected_days": rejected_rows, "rejected_by_reason": rejected_by_reason,
                    "invalid_base_by_reason": invalid_base_by_reason,
        "counter_day_diagnostics": counter_day_diagnostics,
                "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods, "telemetry_incomplete_periods": telemetry_incomplete_periods,
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
        return {"period_diagnostics": period_diagnostics, "status": "unstable", "days": total, "periods": total,
                "telemetry": {"status": "not_evaluated", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                              "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                              "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                              "message": "Model bazowy niestabilny; model telemetrii nie został oceniony"},
                "rejected_days": rejected_rows, "rejected_by_reason": rejected_by_reason,
                    "invalid_base_by_reason": invalid_base_by_reason,
        "counter_day_diagnostics": counter_day_diagnostics,
                "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods,
                "message": "Nie udało się dopasować modelu końcowego"}

    telemetry_result = {"status": "collecting", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                        "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                        "selected_features": telemetry_selected_features,
                        "feature_coverage_periods": telemetry_feature_coverage,
                        "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                        "training_days_needed": TELEMETRY_MIN_TRAIN_PERIODS, "training_periods_needed": TELEMETRY_MIN_TRAIN_PERIODS,
                        "holdout_days_needed": TELEMETRY_MIN_TEST_PERIODS, "holdout_periods_needed": TELEMETRY_MIN_TEST_PERIODS,
                        "message": "Model telemetrii czeka na 20 kompletnych okresów treningowych i 5 testowych"}
    if telemetry_train_count >= TELEMETRY_MIN_TRAIN_PERIODS and telemetry_test_count >= TELEMETRY_MIN_TEST_PERIODS:
        telemetry_baseline_names = ["hdd_lag", "solar_mj", "wind_hdd_lag"]
        telemetry_candidate_names = telemetry_baseline_names + telemetry_selected_features
        telemetry_test = telemetry_clean[telemetry_train_count:]
        telemetry_actual = [float(row["kg_counter"]) for row in telemetry_test]
        base_predictions, extended_predictions = [], []
        for index, row in enumerate(telemetry_test):
            history = telemetry_clean[:telemetry_train_count + index]
            base_fit = _fit(history, telemetry_baseline_names)
            extended_fit = _fit(history, telemetry_candidate_names)
            if base_fit is None or extended_fit is None:
                telemetry_result = {"status": "unstable", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                                    "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
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
                telemetry_result = {"status": "unstable", "complete_days": telemetry_days, "complete_periods": telemetry_periods,
                                    "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                                    "message": "Stały holdout telemetrii nie przeszedł kontroli stabilności"}
            else:
                telemetry_fixed_gain = (
                    (telemetry_fixed_base["mae_kg_day"] - telemetry_fixed_extended["mae_kg_day"])
                    / telemetry_fixed_base["mae_kg_day"]
                    if telemetry_fixed_base["mae_kg_day"] else 0.0
                )
                telemetry_ablation_specs = {
                    "weather_only": telemetry_baseline_names,
                    "weather_plus_temperatures": telemetry_baseline_names + [
                        name for name in (
                            "telemetry_co_c", "telemetry_dhw_c",
                            "telemetry_floor_c", "telemetry_return_c",
                        ) if name in telemetry_selected_features
                    ],
                    "weather_plus_boiler_and_valve": telemetry_baseline_names + [
                        name for name in (
                            "telemetry_boiler_power_kw", "telemetry_valve_opening_pct",
                        ) if name in telemetry_selected_features
                    ],
                    "weather_plus_pumps": telemetry_baseline_names + [
                        name for name in (
                            "telemetry_pump_floor_h", "telemetry_pump_dhw_h",
                            "telemetry_pump_circulation_h",
                        ) if name in telemetry_selected_features
                    ],
                    "weather_plus_all_telemetry": telemetry_candidate_names,
                }
                telemetry_ablation_scores = {
                    label: _fixed_holdout_metrics(
                        telemetry_clean[:telemetry_train_count], telemetry_test, names)
                    for label, names in telemetry_ablation_specs.items()
                }
                telemetry_ablation_available = all(
                    score is not None for score in telemetry_ablation_scores.values()
                )
                telemetry_gain = ((base_score["mae_kg_day"] - extended_score["mae_kg_day"])
                                  / base_score["mae_kg_day"] if base_score["mae_kg_day"] else 0.0)
                telemetry_accepted = (
                    telemetry_gain >= MIN_RELATIVE_GAIN
                    and telemetry_fixed_gain >= MIN_RELATIVE_GAIN
                )
                telemetry_result = {
                    "status": "shadow_ready", "mode": "shadow_only",
                    "complete_days": telemetry_days, "complete_periods": telemetry_periods, "incomplete_days": telemetry_incomplete_days, "incomplete_periods": telemetry_incomplete_periods,
                    "days_needed": telemetry_minimum_days, "periods_needed": telemetry_minimum_periods,
                    "training_days": telemetry_train_count, "training_periods": telemetry_train_count,
                    "holdout_days": telemetry_test_count, "holdout_periods": telemetry_test_count,
                    "holdout_from": telemetry_test[0].get("day"),
                    "holdout_to": telemetry_test[-1].get("day"),
                    "baseline": base_score, "candidate": extended_score,
                    "validation": {
                        "walk_forward": {"baseline": base_score, "candidate": extended_score},
                        "fixed_holdout": {"baseline": telemetry_fixed_base,
                                          "candidate": telemetry_fixed_extended},
                        "periods": [
                            {
                                "period_start": row.get("start"),
                                "period_end": row.get("end"),
                                "period_days": _number(row.get("period_days")),
                                "actual_kg_day": round(actual_rate, 3),
                                "baseline_predicted_kg_day": round(base_rate, 3),
                                "candidate_predicted_kg_day": round(extended_rate, 3),
                                "baseline_error_kg_day": round(base_rate - actual_rate, 3),
                                "candidate_error_kg_day": round(extended_rate - actual_rate, 3),
                            }
                            for row, actual_rate, base_rate, extended_rate in zip(
                                telemetry_test, telemetry_actual, base_predictions, extended_predictions
                            )
                        ],
                    },
                    "mae_gain_percent": round(telemetry_gain * 100, 1),
                    "fixed_holdout_mae_gain_percent": round(telemetry_fixed_gain * 100, 1),
                    "accepted": telemetry_accepted,
                    "selected": "telemetry_enhanced" if telemetry_accepted else "weather_only",
                    "features": telemetry_candidate_names if telemetry_accepted else telemetry_baseline_names,
                    "safety": "shadow_only; production estimates are unchanged",
                    "ablation": {
                        "method": "same chronological fixed holdout; descriptive feature-group comparison, not independent evidence",
                        "available": telemetry_ablation_available,
                        "fixed_holdout": telemetry_ablation_scores,
                    },
                    "validation_note": "walk-forward and fixed holdout currently score the same terminal dates; treat as complementary protocols, not independent samples",
                }

    coefficient_stability = _coefficient_stability(train, selected_names)

    return {
        "period_diagnostics": period_diagnostics,
        "status": "shadow_ready",
        "mode": "shadow_only",
        "coefficient_stability": coefficient_stability,
        "telemetry": telemetry_result,
        "training_days": total, "training_periods": total,
        "holdout_days": test_count, "holdout_periods": test_count,
        "trained_through": clean[-1].get("day"),
        "features": selected_names,
        "selected": "weather_aware" if accepted else "temperature_only",
        "accepted_weather_gain": accepted,
        "fixed_holdout_mae_gain_percent": round(fixed_gain * 100, 1),
        "rejected_days": rejected_rows, "rejected_periods": rejected_rows,
        "rejected_by_reason": rejected_by_reason,
        "invalid_base_by_reason": invalid_base_by_reason,
        "counter_day_diagnostics": counter_day_diagnostics,
        "telemetry_incomplete_days": telemetry_incomplete_days,
        "telemetry_incomplete_periods": telemetry_incomplete_periods,
        "mae_gain_percent": round(gain * 100, 1),
        "baseline": baseline_score,
        "candidate": candidate_score,
        "ablation": {
            "method": "same chronological fixed holdout; descriptive comparison, not independent evidence",
            "available": ablation_available,
            "fixed_holdout": ablation_scores,
        },
        "validation": {
            "protocol": "walk_forward_expanding_window",
            "independence_note": "walk-forward and fixed holdout score the same terminal dates; metrics are complementary, not independent evidence",
            "walk_forward": {"baseline": baseline_score, "candidate": candidate_score},
            "periods": period_comparisons,
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
