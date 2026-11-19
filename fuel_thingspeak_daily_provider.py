"""ThingSpeak daily temperature and burner-energy provider."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone


def fetch_day(*, day, tz, channel_id, read_key, endpoint, outdoor_field,
              power_field, hdd_base, max_gap_seconds, request_json, parse_timestamp):
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    params = {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "results": 8000,
    }
    if read_key:
        params["api_key"] = read_key
    feeds = request_json(
        f"{endpoint}/channels/{channel_id}/feeds.json", params
    ).get("feeds", [])

    temperatures, power = [], []
    for feed in feeds:
        stamp = parse_timestamp(feed["created_at"]).timestamp()
        temperature = feed.get(f"field{outdoor_field}")
        power_kw = feed.get(f"field{power_field}")
        if temperature not in (None, ""):
            temperatures.append(float(temperature))
        if power_kw not in (None, ""):
            power.append((stamp, float(power_kw)))

    power.sort()
    energy_kwh = 0.0
    for (stamp0, power0), (stamp1, _power1) in zip(power, power[1:]):
        elapsed = min(stamp1 - stamp0, max_gap_seconds)
        if elapsed > 0:
            energy_kwh += power0 * elapsed / 3600.0

    mean = sum(temperatures) / len(temperatures) if temperatures else None
    return {
        "day": day.isoformat(),
        "t_mean": round(mean, 2) if mean is not None else None,
        "hdd": round(max(0.0, hdd_base - mean), 2) if mean is not None else None,
        "burner_kwh": round(energy_kwh, 2),
        "samples": len(feeds),
    }


def detect_data_start(*, configured_start, channel_id, read_key, endpoint,
                      request_json, parse_timestamp, timezone):
    if configured_start:
        return date.fromisoformat(configured_start)
    params = {"results": 1}
    if read_key:
        params["api_key"] = read_key
    channel = request_json(
        f"{endpoint}/channels/{channel_id}/feeds.json", params
    ).get("channel", {})
    created_at = channel.get("created_at")
    return parse_timestamp(created_at).astimezone(timezone).date() if created_at else None
