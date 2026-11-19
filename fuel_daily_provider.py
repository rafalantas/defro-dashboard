"""Daily source arbitration and cache policy for the fuel engine."""
from __future__ import annotations


def get_daily_raw(*, day, today, cache, channel_configured, data_start,
                  fetch_thingspeak, openmeteo_mean, hdd_base, fuel_error):
    """Prefer ThingSpeak; use Open-Meteo for missing temperature only."""
    key = day.isoformat()
    if day < today and key in cache:
        return dict(cache[key])

    record = None
    thingspeak_failed = False
    start_date = data_start()
    if channel_configured and (start_date is None or day >= start_date):
        try:
            record = fetch_thingspeak(day)
        except fuel_error:
            thingspeak_failed = True

    if record is None or not record["samples"] or record.get("t_mean") is None:
        temperature = openmeteo_mean(day)
        record = {
            "day": key,
            "t_mean": round(temperature, 2) if temperature is not None else None,
            "hdd": round(max(0.0, hdd_base - temperature), 2)
                   if temperature is not None else None,
            "burner_kwh": record["burner_kwh"] if record and record["samples"] else None,
            "samples": record["samples"] if record else 0,
            "source": "open-meteo",
        }
    else:
        record["source"] = "thingspeak"

    if day < today and record.get("hdd") is not None and not thingspeak_failed:
        cache[key] = record
    if thingspeak_failed:
        record["warning"] = "ThingSpeak niedostępny, temperatura z Open-Meteo"
    return dict(record)
