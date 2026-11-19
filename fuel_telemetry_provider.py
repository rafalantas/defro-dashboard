"""ThingSpeak telemetry-history provider with injected transport and cache ownership.

This module deliberately knows nothing about application globals. The legacy fuel
facade supplies its existing configuration, cache and analysis function so the
extraction does not change endpoint contracts or cache policy.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


def fetch_history(*, days, max_days, channel_ids, read_keys, today, tz,
                  cache, cache_ttl, partial_cache_ttl, fetch_windows,
                  request_json, analyze, endpoint, monotonic):
    """Fetch bounded windows for both channels and preserve partial coverage."""
    days = max(1, min(int(days), max_days))
    cache_key = days
    cached = cache["data"]
    if cached is not None and cache["key"] == cache_key:
        ttl = (partial_cache_ttl if cached.get("fetch_status") == "partial"
               else cache_ttl)
        if monotonic() - cache["at"] < ttl:
            return cached

    if not all(channel_ids):
        raise ValueError("Historia telemetrii wymaga dwóch kanałów THINGSPEAK_CHANNEL_IDS")

    windows = fetch_windows(days, today)
    jobs = [(channel_index, start_day, end_day, offset)
            for channel_index in range(2)
            for start_day, end_day, offset in windows]

    def fetch(job):
        channel_index, start_day, end_day, offset = job
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(end_day, datetime.min.time(), tzinfo=tz)
        params = {
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": str(tz),
            "results": 8000,
        }
        if read_keys[channel_index]:
            params["api_key"] = read_keys[channel_index]
        try:
            payload = request_json(
                f"{endpoint}/channels/{channel_ids[channel_index]}/feeds.json",
                params=params, timeout=12
            )
            return channel_index, offset, payload.get("feeds", []), None
        except Exception as error:
            return channel_index, offset, [], f"{type(error).__name__}: {error}"[:160]

    feeds_by_channel = [[], []]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, jobs))

    errors = []
    for channel_index, _offset, feeds, error in sorted(
            results, key=lambda item: (item[0], -item[1])):
        feeds_by_channel[channel_index].extend(feeds)
        if error:
            errors.append(error)

    result = analyze(feeds_by_channel, channel_ids, days)
    result["fetch_status"] = "partial" if errors else "complete"
    result["fetch_windows"] = {
        "requested": len(jobs),
        "completed": len(jobs) - len(errors),
        "failed": len(errors),
    }
    if errors:
        result["fetch_errors"] = errors[:5]
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    cache.update(key=cache_key, at=monotonic(), data=result)
    return result
