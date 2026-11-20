"""ThingSpeak refill-channel provider with injected cache and invalidation."""
from __future__ import annotations

from datetime import datetime, timezone


def fetch_refills(*, channel_id, read_key, endpoint, request_json, cache,
                  ttl_seconds, monotonic, normalize, tz, parse_timestamp):
    if not channel_id:
        return []
    cached = cache["data"]
    if cached is not None and monotonic() - cache["at"] < ttl_seconds:
        return [dict(item) for item in cached]

    params = {"results": 8000}
    if read_key:
        params["api_key"] = read_key
    feeds = request_json(
        f"{endpoint}/channels/{channel_id}/feeds.json", params
    ).get("feeds", [])
    records = normalize(feeds, tz, parse_timestamp)
    cache.update(at=monotonic(), data=records)
    return [dict(item) for item in records]


def write_refill(*, channel_id, write_key, endpoint, request_json, fields,
                 created_at, fuel_error, invalidate, timezone_module=timezone):
    if not (channel_id and write_key):
        raise fuel_error(503, "Kanał ThingSpeak na dosypki nie jest skonfigurowany")
    form = {"api_key": write_key, **fields}
    if created_at:
        form["created_at"] = created_at.astimezone(timezone_module.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    body = request_json(f"{endpoint}/update.json", form=form)
    entry_id = body.get("entry_id", 0) if isinstance(body, dict) else int(body or 0)
    if not entry_id:
        raise fuel_error(429, "ThingSpeak odrzucił zapis (limit 15 s), spróbuj ponownie za chwilę")
    invalidate()
    return entry_id
