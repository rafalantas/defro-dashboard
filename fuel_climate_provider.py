"""Five-year Open-Meteo monthly climate normals used by planning forecasts."""
from __future__ import annotations

from datetime import timedelta


def climate_monthly(*, today, years, latitude, longitude, timezone,
                    archive_url, cache, cache_seconds, request_json,
                    fuel_error, monotonic):
    cached = cache["data"]
    if cached and monotonic() - cache["at"] < cache_seconds:
        return cached

    end = today - timedelta(days=1)
    start = end.replace(year=end.year - years)
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_mean,shortwave_radiation_sum,wind_speed_10m_max",
        "timezone": str(timezone),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    daily = request_json(archive_url, params).get("daily", {})
    fields = ("temperature_2m_mean", "shortwave_radiation_sum", "wind_speed_10m_max")
    buckets = {}
    for index, day_text in enumerate(daily.get("time", [])):
        bucket = buckets.setdefault(
            int(day_text[5:7]), {name: [] for name in fields}
        )
        for name in fields:
            values = daily.get(name, [])
            value = values[index] if index < len(values) else None
            if value is not None:
                bucket[name].append(float(value))

    result = {}
    for month, values in buckets.items():
        result[month] = {
            "t_mean": sum(values[fields[0]]) / len(values[fields[0]]) if values[fields[0]] else None,
            "solar_mj": sum(values[fields[1]]) / len(values[fields[1]]) if values[fields[1]] else None,
            "wind_max_kmh": sum(values[fields[2]]) / len(values[fields[2]]) if values[fields[2]] else None,
            "samples": len(values[fields[0]]),
        }
    if not result:
        raise fuel_error(502, "Brak danych klimatycznych Open-Meteo dla lokalizacji")
    cache.update(at=monotonic(), data=result)
    return result
