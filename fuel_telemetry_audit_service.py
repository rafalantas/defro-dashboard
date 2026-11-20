"""Uncached, per-local-day ThingSpeak telemetry audit endpoint service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def build_audit(query, *, max_days, channel_ids, read_keys, today, tz,
                endpoint, request_json, analyze, fuel_error, ignition_config=None):
    """Fetch each requested day separately; preserve the legacy audit contract."""
    try:
        days = max(1, min(int(query.get("days", ["7"])[0]), 14))
    except (TypeError, ValueError):
        raise fuel_error(400, "days musi być liczbą") from None

    if len(channel_ids) < 2 or not all(channel_ids[:2]):
        raise fuel_error(503, "Audyt wymaga dwóch kanałów THINGSPEAK_CHANNEL_IDS")

    feeds_by_channel = [[] for _ in channel_ids]
    for channel_index, channel_id in enumerate(channel_ids):
        if not channel_id:
            continue
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
            end = start + timedelta(days=1)
            params = {
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "Europe/Warsaw",
                "results": 8000,
            }
            if channel_index < len(read_keys) and read_keys[channel_index]:
                params["api_key"] = read_keys[channel_index]
            payload = request_json(
                f"{endpoint}/channels/{channel_id}/feeds.json",
                params=params, timeout=12
            )
            feeds_by_channel[channel_index].extend(payload.get("feeds", []))

    if ignition_config is None:
        result = analyze(feeds_by_channel, channel_ids, days)
    else:
        result = analyze(feeds_by_channel, channel_ids, days, ignition_config=ignition_config)
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return result
