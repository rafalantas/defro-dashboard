"""ThingSpeak telemetry-history provider with bounded multi-window cache.

The provider preserves partial coverage and shares in-flight refreshes for the
same (day range, local date) key. Different ranges no longer evict each other.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fuel_errors import FuelError


def fetch_history(*, days, max_days, channel_ids, read_keys, today, tz,
                  cache, cache_ttl, partial_cache_ttl, fetch_windows,
                  request_json, analyze, endpoint, monotonic, singleflight=None,
                  max_cache_entries=4):
    """Fetch bounded windows for both channels and preserve partial coverage."""
    days = max(1, min(int(days), max_days))
    cache_key = (days, today.isoformat())
    entries = cache.setdefault("entries", {})

    def refresh():
        entry = entries.get(cache_key)
        if entry is not None:
            cached = entry["data"]
            ttl = (partial_cache_ttl if cached.get("fetch_status") == "partial"
                   else cache_ttl)
            if monotonic() - entry["at"] < ttl:
                return cached

        if not all(channel_ids):
            raise FuelError(503, "Historia telemetrii wymaga dwóch kanałów THINGSPEAK_CHANNEL_IDS")

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

        entries[cache_key] = {"at": monotonic(), "data": result}
        entries.move_to_end(cache_key) if hasattr(entries, "move_to_end") else None
        while len(entries) > max_cache_entries:
            entries.pop(next(iter(entries)))
        # Keep the legacy fields readable for existing diagnostics/tests.
        cache.update(key=days, at=entries[cache_key]["at"], data=result)
        return result

    if singleflight is not None:
        return singleflight.run(("telemetry-history", cache_key), refresh)
    return refresh()
