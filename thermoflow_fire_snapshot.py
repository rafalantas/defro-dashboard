"""Read a bounded snapshot from the existing private FIRE channel; no local storage."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

SCHEMA = "thermoflow-raw-fire/1"
FIRE = "thermoflow-fire"
LIMIT = 8000
UTC_FORMAT = "%Y-%m-%d %H:%M:%S"


class SnapshotError(Exception):
    """Diagnostic safe to show without exposing the read key or URL."""


def _window(channel, key, start, end, opener):
    params = urlencode({
        "api_key": key, "timezone": "Etc/UTC", "results": LIMIT,
        "start": (start - timedelta(seconds=1)).strftime(UTC_FORMAT),
        "end": end.strftime(UTC_FORMAT),
    })
    url = f"https://api.thingspeak.com/channels/{channel}/fields/1.json?{params}"
    try:
        with opener(url, timeout=15) as response:
            rows = json.load(response)["feeds"]
    except HTTPError as error:
        raise SnapshotError(f"ThingSpeak HTTP {error.code}") from None
    except Exception as error:
        raise SnapshotError(f"ThingSpeak read failed ({type(error).__name__})") from None
    if not isinstance(rows, list) or len(rows) >= LIMIT:
        raise SnapshotError("invalid or truncated ThingSpeak window")

    points = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SnapshotError("invalid ThingSpeak timestamp") from None
        if start <= stamp < end:
            state = str(row.get("field1")).strip()
            points.append([int(stamp.timestamp()), {"0": 0, "0.0": 0, "1": 1, "1.0": 1}.get(state)])
    return points


def read_snapshot(channel, key, *, days, now, opener=urlopen):
    if not channel.isascii() or not channel.isdigit() or not key:
        raise SnapshotError("FIRE channel ID and Read API key must be set in the environment")
    if type(days) is not int or not 1 <= days <= 14 or now.utcoffset() is None:
        raise SnapshotError("use 1–14 days and a timezone-aware clock")
    end = now.astimezone(timezone.utc).replace(microsecond=0)
    day = timedelta(days=1)
    start = end - days * day
    points = []
    while start < end:
        points.extend(_window(channel, key, start, start + day, opener))
        start += day
    points.sort(key=lambda row: row[0])
    return {"schema": SCHEMA, "sampling": "raw", "points": {FIRE: points}}


def summarize_snapshot(snapshot):
    points = snapshot["points"][FIRE]
    fire_off = sum(state == 0 for _, state in points)
    fire_on = sum(state == 1 for _, state in points)
    return {
        "samples": len(points), "fire_off": fire_off, "fire_on": fire_on,
        "invalid_or_missing": len(points) - fire_off - fire_on,
        "first_timestamp": points[0][0] if points else None,
        "last_timestamp": points[-1][0] if points else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--status", action="store_true", help="print counts instead of timestamps and samples")
    args = parser.parse_args(argv)
    try:
        result = read_snapshot(
            os.getenv("THINGSPEAK_FIRE_CHANNEL_ID", "").strip(),
            os.getenv("THINGSPEAK_FIRE_READ_KEY", "").strip(),
            days=args.days, now=datetime.now(timezone.utc))
    except SnapshotError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(summarize_snapshot(result) if args.status else result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
