"""Archive a dedicated ThingSpeak FIRE channel locally without training or writes upstream."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

SCHEMA = "thermoflow-raw-fire/1"
WINDOW = 6 * 3600
OVERLAP = 15 * 60
INITIAL_LOOKBACK = 24 * 3600
POLL_INTERVAL = 300
MAX_WINDOWS_PER_POLL = 12
LIMIT = 8000


class CollectorError(Exception):
    """Safe public error: never contains upstream URLs or payloads."""


def _utc(second):
    return datetime.fromtimestamp(second, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _numeric(raw):
    try:
        result = float(raw)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_window(channel, key, start, end, *, opener=urlopen):
    """Return parsed rows for the half-open UTC interval, or reject the whole window."""
    params = urlencode({
        "api_key": key, "start": _utc(start - 1), "end": _utc(end),
        "timezone": "Etc/UTC", "results": LIMIT,
    })
    url = f"https://api.thingspeak.com/channels/{channel}/feeds.json?{params}"
    try:
        with opener(url, timeout=15) as response:
            feeds = json.load(response)["feeds"]
    except HTTPError as error:
        raise CollectorError(f"ThingSpeak HTTP {error.code}") from None
    except Exception as error:
        raise CollectorError(f"ThingSpeak read failed ({type(error).__name__})") from None
    if not isinstance(feeds, list) or len(feeds) >= LIMIT:
        raise CollectorError("invalid or truncated ThingSpeak window")
    rows = []
    for row in feeds:
        try:
            ts = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            entry_id = row["entry_id"]
            if ts.utcoffset() is None or type(entry_id) is not int or entry_id < 1:
                raise ValueError
            stamp = int(ts.timestamp())
            if start <= stamp < end:
                state = _numeric(row.get("field1"))
                rows.append((entry_id, stamp, int(state) if state in (0, 1) else None,
                             _numeric(row.get("field2"))))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise CollectorError("malformed ThingSpeak entry or timestamp") from None
    return rows


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS fire_samples (
            channel TEXT NOT NULL, entry_id INTEGER NOT NULL, ts INTEGER NOT NULL,
            state INTEGER, brightness REAL, PRIMARY KEY (channel, entry_id)
        );
        CREATE INDEX IF NOT EXISTS fire_samples_time ON fire_samples (channel, ts);
        CREATE TABLE IF NOT EXISTS fire_checkpoint (
            channel TEXT PRIMARY KEY, archived_through INTEGER NOT NULL
        );
    """)


def collect_once(db, channel, key, now, *, fetch=fetch_window):
    """Replay recent overlap and advance only after persisting a successful window."""
    saved = db.execute(
        "SELECT archived_through FROM fire_checkpoint WHERE channel=?", (channel,)).fetchone()
    checkpoint = saved[0] if saved else None
    if checkpoint is not None and checkpoint > now:
        raise CollectorError("host clock is older than the archive checkpoint")
    start = max(1, now - INITIAL_LOOKBACK if checkpoint is None else checkpoint - OVERLAP)
    for _ in range(MAX_WINDOWS_PER_POLL):
        if start >= now:
            break
        end = min(start + WINDOW, now)
        rows = fetch(channel, key, start, end)
        with db:
            db.executemany("""
                INSERT INTO fire_samples VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel, entry_id) DO UPDATE SET
                    ts=excluded.ts, state=excluded.state, brightness=excluded.brightness
            """, ((channel, *row) for row in rows))
            db.execute("""
                INSERT INTO fire_checkpoint VALUES (?, ?)
                ON CONFLICT(channel) DO UPDATE SET
                    archived_through=max(archived_through, excluded.archived_through)
            """, (channel, end))
        start = end
    return archive_status(db, channel)


def archive_status(db, channel):
    last = db.execute(
        "SELECT archived_through FROM fire_checkpoint WHERE channel=?", (channel,)).fetchone()
    samples, valid, last_state = db.execute("""
        SELECT count(*), count(state), max(CASE WHEN state IS NOT NULL THEN ts END)
        FROM fire_samples WHERE channel=?
    """, (channel,)).fetchone()
    return {"archived_through": last[0] if last else None, "samples": samples,
            "valid_fire_samples": valid, "last_valid_fire_timestamp": last_state}


def export_snapshot(db, channel):
    rows = db.execute("""
        SELECT ts, state FROM fire_samples WHERE channel=? ORDER BY ts, entry_id
    """, (channel,))
    return {"schema": SCHEMA, "sampling": "raw", "points": {"thermoflow-fire": list(rows)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "sync-once", "status", "export"))
    parser.add_argument("--db", type=Path, default=Path("/archive/fire.sqlite3"))
    args = parser.parse_args(argv)
    channel = os.getenv("THINGSPEAK_FIRE_CHANNEL_ID", "").strip()
    key = os.getenv("THINGSPEAK_FIRE_READ_KEY", "").strip()
    if not channel.isascii() or not channel.isdigit():
        parser.error("THINGSPEAK_FIRE_CHANNEL_ID is required")
    reading = args.command in {"run", "sync-once"}
    if reading and not key:
        parser.error("THINGSPEAK_FIRE_READ_KEY is required")
    os.umask(0o077)
    try:
        if reading:
            args.db.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(args.db)
            initialize(db)
        else:
            db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
        if args.command in {"status", "export"}:
            print(json.dumps((archive_status if args.command == "status" else export_snapshot)(db, channel)))
            return 0
        while True:
            try:
                result = collect_once(db, channel, key, int(time.time()))
                print(json.dumps({"status": "synced", **result}), flush=True)
            except CollectorError as error:
                print(str(error), file=sys.stderr, flush=True)
                if args.command == "sync-once":
                    return 2
            if args.command == "sync-once":
                return 0
            time.sleep(POLL_INTERVAL)
    except (sqlite3.Error, OSError) as error:
        print(f"FIRE archive unavailable ({type(error).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
