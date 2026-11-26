"""Scheduled, resumable ThingSpeak history ingestion into the shared ThermoFlow SQLite archive.

All upstream operations are GET-only. Raw feed payloads retain source/channel
provenance and original UTC timestamps. This worker never contacts HC3,
changes heating settings, or trains/promotes models.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from thermoflow_fuel_event_store import persist_refill_feeds
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore
from thermoflow_thingspeak_collector import (
    MAX_FEEDS_PER_CHANNEL,
    ThingSpeakHistoryCollector,
)

API_BASE = "https://api.thingspeak.com"
TIMEZONE_NAME = "Europe/Warsaw"
CHUNK_SECONDS = 2 * 86400
OVERLAP_SECONDS = 120
DEFAULT_HISTORY_DAYS = 7
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_REQUEST_INTERVAL_SECONDS = 15
COLLECTOR_NAME = "thingspeak_history"


def _csv_env(env: dict, name: str) -> list[str]:
    raw = str(env.get(name, "") or "").strip()
    return [part.strip() for part in raw.split(",")] if raw else []


def _positive_int(value, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if str(parsed) != str(value).strip() or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("ThingSpeak timestamp must include a timezone")
    return parsed


def _channels_from_env(env: dict) -> tuple[dict, dict, str | None, str | None]:
    """Build channel configuration without returning or logging API keys."""
    ids = _csv_env(env, "THINGSPEAK_CHANNEL_IDS")
    if len(ids) != 2 or any(not item or not item.isdigit() for item in ids):
        raise ValueError("THINGSPEAK_CHANNEL_IDS must contain the two configured channel IDs")
    keys = _csv_env(env, "THINGSPEAK_READ_KEYS")
    keys = (keys + ["", ""])[:2]

    channels = {
        "temperatures_power": {
            "kind": "telemetry", "channel_id": ids[0], "read_key": keys[0],
        },
        "valve_pumps_fuel": {
            "kind": "telemetry", "channel_id": ids[1], "read_key": keys[1],
        },
    }
    fire_id = str(env.get("THINGSPEAK_FIRE_CHANNEL_ID", "") or "").strip()
    fire_key = str(env.get("THINGSPEAK_FIRE_READ_KEY", "") or "").strip()
    if fire_id and not fire_id.isdigit():
        raise ValueError("THINGSPEAK_FIRE_CHANNEL_ID must be numeric")
    if fire_id:
        channels["fire"] = {
            "kind": "telemetry", "channel_id": fire_id, "read_key": fire_key,
        }

    fuel_id = str(env.get("THINGSPEAK_FUEL_CHANNEL_ID", "") or "").strip()
    fuel_key = str(env.get("THINGSPEAK_FUEL_READ_KEY", "") or "").strip()
    if fuel_id and not fuel_id.isdigit():
        raise ValueError("THINGSPEAK_FUEL_CHANNEL_ID must be numeric")
    if fuel_id:
        # Keep the complete raw refill/void feed in telemetry_samples as well
        # as the structured, model-facing refill_events projection.
        channels["fuel_refills_raw"] = {
            "kind": "telemetry", "channel_id": fuel_id, "read_key": fuel_key,
        }
    return channels, {name: item["read_key"] for name, item in channels.items()}, fuel_id or None, fuel_key or None


def _request_json(url: str, params: dict, *, timeout: int = 12) -> dict:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "ThermoFlow-ThingSpeak-Collector"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"ThingSpeak returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("ThingSpeak read request failed") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("feeds"), list):
        raise RuntimeError("ThingSpeak returned an invalid feed response")
    return payload


class _RequestLimiter:
    def __init__(self, *, interval_seconds, wait, monotonic):
        self.interval = interval_seconds
        self.wait = wait
        self.monotonic = monotonic
        self.last_request = None

    def before_request(self):
        now = self.monotonic()
        if self.last_request is not None:
            remaining = self.interval - (now - self.last_request)
            if remaining > 0:
                self.wait(remaining)
        self.last_request = self.monotonic()


def _fetch_channel_window(
    channel_id: str,
    read_key: str,
    start: int,
    end: int,
    *,
    channel_name: str,
    request_json,
    limiter: _RequestLimiter,
    timezone_name: str = TIMEZONE_NAME,
    depth: int = 0,
) -> list[dict]:
    """Fetch a bounded interval, recursively splitting any capped 8000-row response."""
    if end <= start:
        return []
    zone = ZoneInfo(timezone_name)
    params = {
        "start": datetime.fromtimestamp(start, zone).strftime("%Y-%m-%d %H:%M:%S"),
        "end": datetime.fromtimestamp(end, zone).strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": timezone_name,
        "results": MAX_FEEDS_PER_CHANNEL,
    }
    if read_key:
        params["api_key"] = read_key
    limiter.before_request()
    try:
        payload = request_json(f"{API_BASE}/channels/{channel_id}/feeds.json", params)
    except Exception as error:
        # Never propagate a request URL or API key into container logs.
        if isinstance(error, RuntimeError):
            raise RuntimeError(f"ThingSpeak read failed for channel {channel_name}: {error}") from None
        raise RuntimeError(f"ThingSpeak read failed for channel {channel_name}") from None
    feeds = payload["feeds"]
    if len(feeds) < MAX_FEEDS_PER_CHANNEL:
        return feeds
    if end - start <= 60 or depth >= 20:
        raise RuntimeError(f"ThingSpeak channel {channel_name} exceeds the safe feed limit")
    midpoint = (start + end) // 2
    left = _fetch_channel_window(
        channel_id, read_key, start, midpoint,
        channel_name=channel_name, request_json=request_json, limiter=limiter,
        timezone_name=timezone_name, depth=depth + 1,
    )
    right = _fetch_channel_window(
        channel_id, read_key, midpoint + 1, end,
        channel_name=channel_name, request_json=request_json, limiter=limiter,
        timezone_name=timezone_name, depth=depth + 1,
    )
    return left + right


def _cursor_values(store: SQLiteStore, cursor_names: list[str]) -> dict[str, int]:
    if not cursor_names:
        return {}
    placeholders = ",".join("?" for _ in cursor_names)
    with store.connection() as db:
        rows = db.execute(
            f"SELECT collector_name,cursor_value FROM collector_state "
            f"WHERE collector_name IN ({placeholders})",
            cursor_names,
        ).fetchall()
    result = {}
    for row in rows:
        try:
            value = int(row["cursor_value"])
        except (TypeError, ValueError):
            continue
        if value > 0:
            result[row["collector_name"]] = value
    return result


def _record_refill_checkpoint(
    store: SQLiteStore,
    *,
    start: int,
    end: int,
    now: int,
    fetched: int,
    result: dict,
) -> None:
    """Checkpoint the structured refill projection after its idempotent insert."""
    skipped = int(result.get("skipped", 0))
    inserted = int(result.get("inserted", 0))
    unchanged = int(result.get("unchanged", 0))
    status = "complete" if skipped == 0 else "partial"
    details = json.dumps(
        {"source": "thingspeak_fuel", "skipped": skipped},
        sort_keys=True, separators=(",", ":"),
    )
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO collection_runs(
                collector_name,started_at,completed_at,window_start,window_end,
                fetched,inserted,duplicates,invalid,status,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{COLLECTOR_NAME}:refills", now, now, start, end,
                fetched, inserted, unchanged, skipped, status, details,
            ),
        )
        if status == "complete":
            db.execute(
                """INSERT INTO collector_state(
                    collector_name,cursor_value,updated_at,metadata_json
                ) VALUES(?,?,?,?)
                ON CONFLICT(collector_name) DO UPDATE SET
                    cursor_value=CASE
                        WHEN CAST(excluded.cursor_value AS INTEGER) >
                             CAST(collector_state.cursor_value AS INTEGER)
                        THEN excluded.cursor_value
                        ELSE collector_state.cursor_value
                    END,
                    updated_at=excluded.updated_at""",
                (f"{COLLECTOR_NAME}:refills", str(end), now, "{}"),
            )


def run_once(
    database_path: str = DEFAULT_DB_PATH,
    *,
    now: int | None = None,
    env: dict | None = None,
    request_json=_request_json,
    wait=time.sleep,
    monotonic=time.monotonic,
) -> dict:
    """Ingest one resumable window from configured channels into the shared DB."""
    clock = int(time.time()) if now is None else now
    if type(clock) is not int or clock <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    config = os.environ if env is None else env
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")

    try:
        channels, read_keys, fuel_id, fuel_key = _channels_from_env(config)
    except ValueError as error:
        return {
            "schema": "thermoflow-thingspeak-history/1",
            "status": "blocked",
            "reason": str(error),
            "read_only": True,
        }

    history_days = _positive_int(
        config.get("THERMOFLOW_THINGSPEAK_HISTORY_DAYS", DEFAULT_HISTORY_DAYS),
        name="THERMOFLOW_THINGSPEAK_HISTORY_DAYS", minimum=1, maximum=30,
    )
    initial_raw = str(config.get("THERMOFLOW_THINGSPEAK_INITIAL_START", "") or "").strip()
    if initial_raw:
        initial_start = _positive_int(
            initial_raw, name="THERMOFLOW_THINGSPEAK_INITIAL_START",
            minimum=1, maximum=253402300799,
        )
    else:
        initial_start = max(1, clock - history_days * 86400)
    request_interval = float(config.get(
        "THERMOFLOW_THINGSPEAK_REQUEST_INTERVAL_SECONDS",
        DEFAULT_REQUEST_INTERVAL_SECONDS,
    ))
    if not math.isfinite(request_interval) or not 0 <= request_interval <= 60:
        raise ValueError("THERMOFLOW_THINGSPEAK_REQUEST_INTERVAL_SECONDS must be between 0 and 60")

    store = SQLiteStore(path)
    collector = ThingSpeakHistoryCollector(store)
    cursor_names = [f"{COLLECTOR_NAME}:{name}" for name in channels]
    if fuel_id:
        cursor_names.append(f"{COLLECTOR_NAME}:refills")
    cursors = _cursor_values(store, cursor_names)
    if len(cursors) < len(cursor_names):
        start = min([initial_start, *cursors.values()])
    else:
        start = max(1, min(cursors.values()) - OVERLAP_SECONDS)
    end = min(clock, start + CHUNK_SECONDS)
    if end <= start:
        return {
            "schema": "thermoflow-thingspeak-history/1",
            "status": "idle",
            "reason": "cursor_is_current",
            "cursors": cursors,
            "read_only": True,
        }

    limiter = _RequestLimiter(
        interval_seconds=request_interval, wait=wait, monotonic=monotonic,
    )
    fetched_channels = {}
    feeds_by_name = {}
    for name, channel in channels.items():
        feeds = _fetch_channel_window(
            str(channel["channel_id"]), read_keys[name], start, end,
            channel_name=name, request_json=request_json, limiter=limiter,
        )
        feeds_by_name[name] = feeds
        fetched_channels[name] = {
            "kind": "telemetry",
            "channel_id": str(channel["channel_id"]),
            "feeds": feeds,
        }

    telemetry_result = collector.save_batch(
        fetched_channels, start=start, end=end, collected_at=clock,
    )
    refill_result = None
    if fuel_id:
        refill_feeds = feeds_by_name.get("fuel_refills_raw", [])
        refill_result = persist_refill_feeds(
            refill_feeds,
            database_path=path,
            tz=ZoneInfo(TIMEZONE_NAME),
            parse_timestamp=_parse_timestamp,
        )
        _record_refill_checkpoint(
            store, start=start, end=end, now=clock,
            fetched=len(refill_feeds), result=refill_result,
        )

    return {
        "schema": "thermoflow-thingspeak-history/1",
        "status": (
            "complete"
            if telemetry_result["status"] == "complete"
            and (refill_result is None or refill_result.get("skipped", 0) == 0)
            else "partial"
        ),
        "window": {"start": start, "end": end},
        "channels": {
            name: {
                "channel_id": str(channel["channel_id"]),
                "fetched": len(feeds_by_name[name]),
            }
            for name, channel in channels.items()
        },
        "telemetry": telemetry_result,
        "refills": refill_result,
        "cursors": _cursor_values(store, cursor_names),
        "safety": {
            "read_only_source": True,
            "database_modified_only_by_ingestion": True,
            "external_requests": True,
            "http_methods": ["GET"],
            "hc3_requests": False,
            "device_commands_sent": False,
            "heating_settings_changed": False,
            "model_training": False,
            "model_promotion": False,
        },
    }


def run_scheduler(
    cycle,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    max_cycles: int | None = None,
    wait=time.sleep,
) -> list[dict]:
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 3600:
        raise ValueError("interval_seconds must be between 60 and 3600")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    results = []
    while max_cycles is None or len(results) < max_cycles:
        try:
            result = cycle()
        except Exception as error:
            result = {
                "schema": "thermoflow-thingspeak-history/1",
                "status": "error",
                "error_type": type(error).__name__,
            }
        if max_cycles is not None:
            results.append(result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        if max_cycles is None or len(results) < max_cycles:
            wait(interval_seconds)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", default=DEFAULT_DB_PATH)
    parser.add_argument("--interval-seconds", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.database).expanduser()
    if not path.is_file():
        parser.error("database file does not exist; initialize the shared SQLite database first")
    interval = args.interval_seconds
    if interval is None:
        interval = _positive_int(
            os.getenv("THERMOFLOW_THINGSPEAK_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS),
            name="THERMOFLOW_THINGSPEAK_INTERVAL_SECONDS", minimum=60, maximum=3600,
        )
    run_scheduler(
        lambda: run_once(str(path)),
        interval_seconds=interval,
        max_cycles=1 if args.once else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
