"""Deterministic last-seven-days and hopper summary calculations.

All I/O, locking, cache ownership and model-specific functions are injected by
the service facade. Payload keys and legacy arithmetic are intentionally kept.
"""
from __future__ import annotations

from datetime import date, datetime


def build_last7_estimate(
    last7: list[dict], model: dict | None, calibration: dict, counter: dict,
    counter_epoch: datetime, day_bounds, estimate_burner_kg, counter_kg,
    predict_kg_per_day,
) -> dict | None:
    """Build the legacy last-seven-complete-days estimate."""
    with_burner = [
        row for row in last7
        if row.get("burner_kwh") is not None and row.get("hdd") is not None
    ]
    if with_burner:
        full = max(row.get("samples", 0) for row in with_burner)
        with_burner = [
            row for row in with_burner if row.get("samples", 0) >= 0.7 * full
        ]
    if not with_burner:
        return None

    kg = sum(
        estimate_burner_kg(
            row["burner_kwh"], day_bounds(date.fromisoformat(row["day"]))[0]
        )
        for row in with_burner
    )
    first = day_bounds(date.fromisoformat(with_burner[0]["day"]))[0]
    last_day = day_bounds(date.fromisoformat(with_burner[-1]["day"]))[1]
    after_epoch = last_day > counter_epoch
    source = (
        "moc palnika (szacunek)"
        if after_epoch and calibration["new"].get("provisional")
        else "moc palnika"
    )
    measured_kg, ok, floor_hit = counter_kg(first, last_day, counter)
    if ok and not floor_hit and measured_kg:
        kg = measured_kg
        source = (
            "licznik podajnika (szacunek)"
            if after_epoch and counter["collecting"]
            else "licznik podajnika"
        )

    hdd = sum(row["hdd"] for row in with_burner)
    result = {
        "days": len(with_burner),
        "kg": round(kg, 1),
        "kg_per_day": round(kg / len(with_burner), 1),
        "hdd": round(hdd, 1),
        "kg_per_hdd": round(kg / hdd, 2) if hdd > 0 else None,
        "t_mean": round(sum(row["t_mean"] for row in with_burner) / len(with_burner), 1),
        "source": source,
    }
    if model:
        result["model_kg"] = round(
            sum(predict_kg_per_day(model, row["t_mean"]) for row in with_burner), 1
        )
        heat = kg - model["base_kg_per_day"] * len(with_burner)
        result["heating_kg_per_hdd"] = round(heat / hdd, 2) if hdd >= 5 else None
    return result


def build_last_refill_and_hopper(
    now: datetime, refills: list[dict], counter: dict, calibration: dict,
    model: dict | None, recent_periods: list[dict], week: dict | None,
    forecast: list, daily_model: dict | None, known_temps: dict,
    period_sums, counter_kg, burner_kg, counter_method, select_estimate,
    days_until, capacity: float, reserve_percent: float,
) -> tuple[dict | None, dict | None]:
    """Build legacy last-refill metadata and hopper/reserve estimate."""
    if not refills:
        return None, None

    last_refill = refills[-1]
    last_ts = datetime.fromisoformat(last_refill["ts"])
    hours = (now - last_ts).total_seconds() / 3600
    last = {
        "ts": last_refill["ts"],
        "kg": last_refill["kg"],
        "hours_ago": round(hours, 1),
    }

    hdd_since, kwh, _hm, kwh_missing = period_sums(last_ts, now)
    kg_since, drop_ok, drop_floor = counter_kg(last_ts, now, counter)
    burner_since = (
        burner_kg(last_ts, now, calibration)
        if not kwh_missing and calibration["calibrated"] else None
    )
    used_counter = kg_since if drop_ok and not drop_floor else None
    used_burner = burner_since
    used_model = (
        model["base_kg_per_day"] * hours / 24 + model["kg_per_hdd"] * hdd_since
        if model else None
    )
    fallback_kg = (
        recent_periods[-1]["kg_per_day"] * hours / 24 if recent_periods else None
    )
    used, method = select_estimate(
        counter_kg=used_counter,
        counter_collecting=counter["collecting"],
        counter_method=counter_method(counter),
        burner_kg=used_burner,
        burner_calibration_days=calibration["days"],
        model_kg=used_model,
        fallback_kg=fallback_kg,
    )

    hopper = None
    if used is not None:
        reserve = capacity * reserve_percent / 100
        remaining = max(0.0, capacity - used)
        to_reserve = max(0.0, remaining - reserve)
        days_to_reserve, rate_source = None, None
        if model and forecast:
            days_to_reserve = days_until(
                to_reserve, model, forecast, now, daily_model, known_temps
            )
            rate_source = "prognoza pogody i model domu"
        if days_to_reserve is None:
            rate = (week or {}).get("kg_per_day") or (
                recent_periods[-1]["kg_per_day"] if recent_periods else None
            )
            days_to_reserve = round(to_reserve / rate, 1) if rate else None
            rate_source = "średnie zużycie z ostatnich dni"
        hopper = {
            "capacity_kg": capacity,
            "reserve_kg": round(reserve, 1),
            "used_kg": round(used, 1),
            "used_kg_counter": round(used_counter, 1) if used_counter is not None else None,
            "counter_note": counter["message"],
            "counter_provisional": used_counter is not None and counter["collecting"],
            "used_kg_model": round(used_model, 1) if used_model is not None else None,
            "used_kg_burner": round(used_burner, 1) if used_burner is not None else None,
            "remaining_kg": round(remaining, 1),
            "remaining_percent": round(100 * remaining / capacity) if capacity else None,
            "to_reserve_kg": round(to_reserve, 1),
            "below_reserve": remaining <= reserve,
            "days_to_reserve": days_to_reserve,
            "days_source": rate_source,
            "method": method,
        }
    return last, hopper
