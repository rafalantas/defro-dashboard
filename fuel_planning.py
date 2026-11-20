"""Pure fuel forecasting and hopper planning calculations.

Weather retrieval, cache ownership and environment configuration are injected by
the legacy service adapter so this module remains deterministic in tests.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def rate_for(model: dict, daily_model, day: date, temperature: float, previous_temperature,
             weather_for_day, predict_daily, predict_kg_per_day, lagged_temperature) -> float:
    """Estimate kg/day using the accepted daily model when available, else the HDD house model."""
    if daily_model and daily_model.get("status") == "aktywny" and daily_model["terms"] != ["hdd"]:
        weather = weather_for_day(day)
        effective = temperature
        if "hdd_lag" in daily_model["terms"]:
            effective = lagged_temperature(
                temperature,
                previous_temperature if previous_temperature is not None else temperature,
            )
        return predict_daily(
            daily_model,
            max(0.0, daily_model["balance_temp"] - effective),
            weather.get("solar_mj"),
            weather.get("wind_max_kmh"),
        )
    return predict_kg_per_day(model, temperature)


def days_until(kg_needed: float, model: dict, forecast: list, now: datetime,
               daily_model=None, known_temps=None, day_bounds=None, rate_for_day=None):
    """Estimate fractional days to consume kg_needed, following the supplied weather forecast."""
    if kg_needed <= 0:
        return 0.0
    by_day = dict(forecast)
    temperatures = {**(known_temps or {}), **by_day}
    elapsed, day = 0.0, now.date()
    fraction = (day_bounds(day)[1] - now).total_seconds() / 86400
    last_temperature = forecast[-1][1] if forecast else None
    for _ in range(60):
        temperature = by_day.get(day, last_temperature)
        if temperature is None:
            return None
        temperatures[day] = temperature
        rate = rate_for_day(
            model, daily_model, day, temperature,
            temperatures.get(day - timedelta(days=1)),
        )
        if rate <= 0:
            return None
        if kg_needed <= rate * fraction:
            return round(elapsed + kg_needed / rate, 1)
        kg_needed -= rate * fraction
        elapsed += fraction
        day += timedelta(days=1)
        fraction = 1.0
    return None


def planning_forecast(model: dict, daily_model, forecast: list, now: datetime,
                      known_temps: dict, climate: dict, weather_for_day,
                      open_meteo_offset, predict_daily, predict_kg_per_day,
                      lagged_temperature, location: str, latitude: float,
                      longitude: float, climate_years: int) -> dict:
    """Build today/week/month/season pellet planning from forecast and climate normals."""
    if not model:
        return {"status": "unavailable", "message": "Za mało danych z dosypek do prognozy"}
    forecast_map = dict(forecast)
    today = now.date()
    month_end = today + timedelta(days=29)
    if 5 <= now.month < 10:
        season_start, season_end = date(now.year, 10, 1), date(now.year + 1, 4, 30)
    else:
        season_start = today
        season_end = date(now.year, 4, 30) if now.month <= 4 else date(now.year + 1, 4, 30)
    horizon = max(month_end, season_end)
    temperatures = {}
    for offset in range((horizon - today).days + 1):
        day = today + timedelta(days=offset)
        if day in forecast_map:
            temperature = forecast_map[day]
            weather = weather_for_day(day)
            solar, wind, source = weather.get("solar_mj"), weather.get("wind_max_kmh"), "prognoza Open-Meteo"
        else:
            normal = climate.get(day.month, {})
            temperature = normal.get("t_mean")
            solar, wind, source = normal.get("solar_mj"), normal.get("wind_max_kmh"), "średnia klimatyczna 5 lat"
            if temperature is not None:
                temperature += open_meteo_offset()["offset"]
        if temperature is None:
            return {"status": "unavailable", "message": "Niekompletne dane pogodowe do prognozy"}
        temperatures[day] = (temperature, solar, wind, source)

    rows, previous = [], known_temps.get(today - timedelta(days=1))
    for offset in range((horizon - today).days + 1):
        day = today + timedelta(days=offset)
        temperature, solar, wind, source = temperatures[day]
        if daily_model and daily_model.get("status") == "aktywny":
            effective = temperature
            if "hdd_lag" in daily_model.get("terms", []):
                effective = lagged_temperature(
                    temperature, previous if previous is not None else temperature,
                )
            kg = predict_daily(
                daily_model,
                max(0.0, daily_model["balance_temp"] - effective),
                solar, wind,
            )
        else:
            kg = predict_kg_per_day(model, temperature)
        rows.append({"day": day.isoformat(), "kg": max(0.0, kg),
                     "t_mean": temperature, "source": source})
        previous = temperature

    def total(start: date, end: date):
        selected = [row for row in rows if start <= date.fromisoformat(row["day"]) <= end]
        kg = sum(row["kg"] for row in selected)
        return {
            "from": start.isoformat(), "to": end.isoformat(), "days": len(selected),
            "kg": round(kg, 1), "bags_15kg": int((kg + 14.999) // 15),
            "average_kg_day": round(kg / len(selected), 2) if selected else None,
        }

    return {
        "status": "ready", "location": location,
        "coordinates": {"latitude": float(latitude), "longitude": float(longitude)},
        "weather": "prognoza 7 dni + średnie miesięczne z 5 lat",
        "climate_years": climate_years, "generated_at": now.isoformat(),
        "today": total(today, today), "week": total(today, today + timedelta(days=6)),
        "month": total(today, month_end), "season": total(season_start, season_end),
        "daily": rows[:7],
        "note": "Orientacyjnie; po 7 dniach używane są średnie klimatyczne. Worki zaokrąglone w górę do 15 kg.",
    }
