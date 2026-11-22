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
    balance_temp = float((daily_model or {}).get("balance_temp", model.get("balance_temp", 15.5)))
    observed = sorted((day, float(temp)) for day, temp in known_temps.items()
                      if temp is not None and day <= today)
    season_start = None
    for index in range(13, len(observed)):
        previous = observed[index - 13:index - 6]
        window = observed[index - 6:index + 1]
        consecutive = all((observed[pos][0] - observed[pos - 1][0]).days == 1
                         for pos in range(index - 13, index + 1))
        if (consecutive and all(item[1] >= balance_temp for item in previous)
                and all(item[1] < balance_temp for item in window)):
            season_start = window[0][0]
    season_end = None
    warm_threshold = balance_temp + 1.0
    for month_offset in range(1, 14):
        month_index = today.month - 1 + month_offset
        year = today.year + month_index // 12
        month = month_index % 12 + 1
        next_index = month_index + 1
        next_month = next_index % 12 + 1
        first = climate.get(month, {}).get("t_mean")
        second = climate.get(next_month, {}).get("t_mean")
        if first is not None and second is not None and float(first) >= warm_threshold and float(second) >= warm_threshold:
            season_end = date(year, month, 1)
            break
    season_start_detected = season_start is not None
    season_end_detected = season_end is not None and season_end > today
    if season_start is None:
        season_start = date(today.year, 10, 1) if today.month >= 10 else (date(today.year - 1, 10, 1) if today.month <= 4 else today)
    if season_end is None or season_end <= today:
        season_end = date(today.year, 4, 30) if today.month <= 4 else date(today.year + 1, 4, 30)
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
        "month": total(today, month_end), "season": total(today, season_end),
        "season_detection": {"start": season_start.isoformat(), "end": season_end.isoformat(), "method": "temperature_history_and_climate" if observed else "calendar_fallback", "balance_temp": balance_temp},
        "daily": rows[:7],
        "note": "Orientacyjnie; po 7 dniach używane są średnie klimatyczne. Worki zaokrąglone w górę do 15 kg.",
    }
