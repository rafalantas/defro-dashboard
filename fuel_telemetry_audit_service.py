"""Bounded, parallel per-local-day ThingSpeak telemetry audit service."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 45 requests are needed for the maximum 14-day audit (15 days × 3 channels).
# Nine workers and an eight-second per-request timeout keep the worst-case
# network wait below the dashboard's 60-second Nginx proxy timeout.
AUDIT_MAX_WORKERS = 9
AUDIT_REQUEST_TIMEOUT_SECONDS = 8


def build_audit(query, *, max_days, channel_ids, read_keys, today, tz,
                endpoint, request_json, analyze, fuel_error, ignition_config=None):
    """Fetch each requested day concurrently while preserving chronological feeds."""
    try:
        days = max(1, min(int(query.get("days", ["7"])[0]), min(14, max_days)))
    except (TypeError, ValueError):
        raise fuel_error(400, "days musi być liczbą") from None

    if len(channel_ids) < 2 or not all(channel_ids[:2]):
        raise fuel_error(503, "Audyt wymaga dwóch kanałów THINGSPEAK_CHANNEL_IDS")

    tasks = []
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
            tasks.append((
                channel_index, offset,
                f"{endpoint}/channels/{channel_id}/feeds.json", params,
            ))

    # Store results by channel/day, then append in chronological order. This
    # avoids completion-order jitter in event detection and daily aggregates.
    results = {}
    channel_errors = {}
    with ThreadPoolExecutor(max_workers=AUDIT_MAX_WORKERS) as executor:
        future_to_key = {
            executor.submit(
                request_json, url, params=params,
                timeout=AUDIT_REQUEST_TIMEOUT_SECONDS,
            ): (channel_index, offset)
            for channel_index, offset, url, params in tasks
        }
        for future in as_completed(future_to_key):
            channel_index, offset = future_to_key[future]
            try:
                payload = future.result()
            except Exception as error:
                # TEMPS and VALVE are required. FIRE (and any later optional
                # channel) must not take down the audit when its read key or
                # ThingSpeak channel is unavailable.
                if channel_index < 2:
                    raise
                channel_errors.setdefault(channel_index, []).append({
                    "day_offset": offset,
                    "error": f"{type(error).__name__}: {getattr(error, 'message', str(error))}"[:200],
                })
                continue
            results[(channel_index, offset)] = payload.get("feeds", [])

    feeds_by_channel = [[] for _ in channel_ids]
    for channel_index, channel_id in enumerate(channel_ids):
        if not channel_id:
            continue
        for offset in range(days, -1, -1):
            feeds_by_channel[channel_index].extend(results[(channel_index, offset)])

    if ignition_config is None:
        result = analyze(feeds_by_channel, channel_ids, days)
    else:
        result = analyze(feeds_by_channel, channel_ids, days, ignition_config=ignition_config)
    fetch_status = {}
    for channel_index, channel_id in enumerate(channel_ids):
        if not channel_id:
            continue
        errors = channel_errors.get(channel_index, [])
        if not errors:
            status = "ok"
        elif len(errors) >= days + 1:
            status = "unavailable"
        else:
            status = "partial"
        key = ("temps", "valve", "fire")[channel_index] if channel_index < 3 else f"channel_{channel_index + 1}"
        fetch_status[key] = {
            "status": status,
            "failed_days": [
                (today - timedelta(days=item["day_offset"])).isoformat()
                for item in errors
            ],
            "errors": [item["error"] for item in errors],
        }

    result["channel_fetch_status"] = fetch_status
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return result
