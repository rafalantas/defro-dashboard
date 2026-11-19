"""ThingSpeak hopper-counter history provider."""
from __future__ import annotations

from datetime import timezone


def fetch_counter_points(*, day, today, cache, channel_id, field_number,
                         read_key, endpoint, tz, request_json, parse_timestamp):
    key = day.isoformat()
    if day < today and key in cache:
        return cache[key]
    if not channel_id:
        return []

    start = __import__("datetime").datetime.combine(
        day, __import__("datetime").time.min, tzinfo=tz
    )
    end = start + __import__("datetime").timedelta(days=1)
    params = {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "results": 8000,
    }
    if read_key:
        params["api_key"] = read_key
    feeds = request_json(
        f"{endpoint}/channels/{channel_id}/fields/{field_number}.json", params
    ).get("feeds", [])
    points = []
    for feed in feeds:
        raw = feed.get(f"field{field_number}")
        if raw in (None, ""):
            continue
        stamp, value = parse_timestamp(feed["created_at"]).timestamp(), float(raw)
        if not points or points[-1][1] != value:
            points.append((stamp, value))
    if feeds and points and points[-1][0] != parse_timestamp(
            feeds[-1]["created_at"]).timestamp():
        points.append((parse_timestamp(feeds[-1]["created_at"]).timestamp(),
                       points[-1][1]))
    if day < today and feeds:
        cache[key] = points
    return points
