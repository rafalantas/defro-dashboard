"""Safe projection of the existing weather-driven fuel planner for Intelligence."""
from __future__ import annotations

from datetime import date, datetime
from math import isfinite

SCHEMA = "thermoflow-weather-forecast/1"
MAX_AGE_SECONDS = 6 * 60 * 60
MAX_DAYS = 7


def _finite(value, low, high):
    return (type(value) in (int, float) and isfinite(value)
            and low <= value <= high)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


def project_weather_forecast(summary, *, now):
    """Expose only fresh, validated rows produced by the existing fuel planner.

    This function never fetches weather or trains a model. The planner remains
    the sole owner of Open-Meteo retrieval, house calibration and prediction.
    """
    empty = {"schema": SCHEMA, "status": "collecting", "reason": "forecast_unavailable",
             "generated_at": None, "model": None, "days": [], "week": None}
    if not isinstance(summary, dict):
        return empty
    stamp = _timestamp(summary.get("fetched_at"))
    if stamp is None or not isfinite(now) or stamp > now:
        return {**empty, "status": "unavailable", "reason": "invalid_forecast_timestamp"}
    age = now - stamp
    if age > MAX_AGE_SECONDS:
        return {**empty, "status": "stale", "reason": "fuel_summary_stale",
                "generated_at": int(stamp)}
    raw = summary.get("forecast")
    if not isinstance(raw, list):
        return {**empty, "status": "unavailable", "reason": "invalid_forecast"}
    rows, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = item.get("day")
        try:
            parsed_day = date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if parsed_day.isoformat() != day or day in seen:
            continue
        if not (_finite(item.get("t_mean"), -90, 65)
                and _finite(item.get("kg"), 0, 1000)):
            continue
        solar, wind = item.get("solar_mj"), item.get("wind_max_kmh")
        if solar is not None and not _finite(solar, 0, 60):
            continue
        if wind is not None and not _finite(wind, 0, 250):
            continue
        seen.add(day)
        rows.append({
            "day": day, "t_mean": round(float(item["t_mean"]), 1),
            "kg": round(float(item["kg"]), 2),
            "solar_mj": round(float(solar), 1) if solar is not None else None,
            "wind_max_kmh": round(float(wind), 1) if wind is not None else None,
        })
    rows.sort(key=lambda row: row["day"])
    rows = rows[:MAX_DAYS]
    if not rows:
        return {**empty, "reason": "no_valid_forecast_days", "generated_at": int(stamp)}

    daily_model = summary.get("daily_model")
    house_model = summary.get("model")
    if isinstance(daily_model, dict) and daily_model.get("status") == "aktywny":
        terms = daily_model.get("terms")
        terms = [name for name in terms if name in ("hdd", "hdd_lag", "solar", "wind_hdd",
                                                       "wind_hdd_lag")] if isinstance(terms, list) else []
        model = {"kind": "weather_daily_model", "training_days": daily_model.get("days"),
                 "terms": terms,
                 "loo_rmse_kg_per_day": daily_model.get("rmse_loo_kg_per_day")
                 if _finite(daily_model.get("rmse_loo_kg_per_day"), 0, 1000) else None}
    elif isinstance(house_model, dict):
        model = {"kind": "temperature_hdd_house_model", "training_days": house_model.get("periods_used"),
                 "terms": ["hdd"], "loo_rmse_kg_per_day": None}
    else:
        model = {"kind": "unavailable", "training_days": None, "terms": [],
                 "loo_rmse_kg_per_day": None}

    total = sum(row["kg"] for row in rows)
    return {
        "schema": SCHEMA, "status": "ready", "reason": None,
        "generated_at": int(stamp), "age_seconds": int(age),
        "source": "Open-Meteo forecast + existing ThermoFlow fuel planner",
        "model": model, "days": rows,
        "week": {"days": len(rows), "kg": round(total, 1),
                 "average_kg_per_day": round(total / len(rows), 2)},
    }
