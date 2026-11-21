"""Open-Meteo temperature and weather provider.

All network access, location settings, caches and clocks are injected by the
legacy facade. The provider preserves the historical fallback and forecast
refresh rules while remaining independently testable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def openmeteo_mean(*, day, latitude, longitude, timezone, archive_url,
                   forecast_url, cache, request_json, fuel_error, now):
    key = day.isoformat()
    if key in cache or not (latitude and longitude):
        return cache.get(key)
    today = now().date()
    common = {"latitude": latitude, "longitude": longitude,
              "daily": "temperature_2m_mean", "timezone": str(timezone)}

    # Values from *this* response. Reading the requested day from here (and
    # not back from the shared cache) keeps concurrent callers for "today"
    # from popping each other's freshly fetched value.
    fetched = {}

    def fill(url, params):
        try:
            daily = request_json(url, params).get("daily", {})
        except fuel_error:
            return
        for stamp, value in zip(daily.get("time", []),
                                daily.get("temperature_2m_mean", [])):
            if value is not None:
                fetched[stamp] = float(value)
                cache[stamp] = float(value)

    if day < today - timedelta(days=7):
        start = day.replace(day=1)
        month_end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        end = min(month_end, today - timedelta(days=6))
        fill(archive_url, {**common, "start_date": start.isoformat(),
                           "end_date": end.isoformat()})
    else:
        fill(forecast_url, {**common, "past_days": 14, "forecast_days": 1})
    if day >= today:
        # Today's forecast changes during the day; do not freeze it in cache.
        shared = cache.pop(key, None)
        return fetched[key] if key in fetched else shared
    return fetched.get(key, cache.get(key))


def weather_for_day(*, day, latitude, longitude, timezone, archive_url,
                    forecast_url, variables, cache, fetched, cache_seconds,
                    request_json, fuel_error, now, monotonic):
    key = day.isoformat()
    today = now().date()
    if not (latitude and longitude):
        return {}
    if key in cache:
        fresh = (day < today or
                 monotonic() - fetched.get(key, 0.0) < cache_seconds)
        if fresh:
            return cache[key]

    common = {"latitude": latitude, "longitude": longitude,
              "daily": variables, "timezone": str(timezone)}

    def fill(url, params):
        daily = request_json(url, params).get("daily", {})
        today_key = now().date().isoformat()
        solar = daily.get("shortwave_radiation_sum", [])
        wind = daily.get("wind_speed_10m_max", [])
        for index, stamp in enumerate(daily.get("time", [])):
            record = {
                "solar_mj": solar[index] if index < len(solar) else None,
                "wind_max_kmh": wind[index] if index < len(wind) else None,
            }
            if record["solar_mj"] is not None or record["wind_max_kmh"] is not None:
                cache[stamp] = record
                if stamp >= today_key:
                    fetched[stamp] = monotonic()

    try:
        if day < today - timedelta(days=7):
            start = day.replace(day=1)
            month_end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            end = min(month_end, today - timedelta(days=6))
            fill(archive_url, {**common, "start_date": start.isoformat(),
                               "end_date": end.isoformat()})
        else:
            fill(forecast_url, {**common, "past_days": 14, "forecast_days": 7})
    except fuel_error:
        return cache.get(key, {})
    return cache.get(key, {})
