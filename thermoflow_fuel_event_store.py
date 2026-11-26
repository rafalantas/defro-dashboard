"""Persistence adapters for existing ThingSpeak refill and Open-Meteo forecast data.

These writers archive source evidence in the shared ThermoFlow SQLite database.
They never fetch external data, change fuel estimates, or control heating devices.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

REFILL_SOURCE = "thingspeak_fuel"
WEATHER_SOURCE = "open-meteo"


def _canonical_payload(payload: dict) -> tuple[str, str]:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _positive_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0 or str(parsed) != str(value).strip():
        return None
    return parsed


def _finite_number(value, *, minimum=None, maximum=None):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _store(database_path):
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("shared ThermoFlow SQLite database does not exist")
    return SQLiteStore(path)


def _occurred_at(value, *, tz, parse_timestamp):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parse_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    try:
        stamp = int(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if stamp > 0 else None


def persist_refill_feeds(feeds, *, database_path=DEFAULT_DB_PATH, tz,
                         parse_timestamp) -> dict:
    """Archive new refill and void feed entries; event IDs make ingestion idempotent."""
    if not isinstance(feeds, (list, tuple)):
        raise ValueError("feeds must be a list")
    store = _store(database_path)
    prepared = []
    skipped = 0
    for feed in feeds:
        if not isinstance(feed, dict):
            skipped += 1
            continue
        entry_id = _positive_int(feed.get("entry_id"))
        occurred_at = _occurred_at(
            feed.get("created_at"), tz=tz, parse_timestamp=parse_timestamp
        )
        kg = _finite_number(feed.get("field1"), minimum=0.001, maximum=1000.0)
        voids_entry_id = _positive_int(feed.get("field3"))
        if entry_id is None or occurred_at is None or (kg is None and voids_entry_id is None):
            skipped += 1
            continue
        payload = {
            "schema": "thermoflow-refill-source-event/1",
            "source_event_id": entry_id,
            "source_created_at": feed.get("created_at"),
            "kg": round(kg, 3) if kg is not None else None,
            "note": str(feed.get("field2") or "")[:255],
            "voids_entry_id": voids_entry_id,
        }
        payload_json, digest = _canonical_payload(payload)
        prepared.append((
            f"thingspeak:fuel:{entry_id}", occurred_at, REFILL_SOURCE,
            payload_json, digest, entry_id,
        ))

    if not prepared:
        return {"status": "empty", "inserted": 0, "unchanged": 0, "skipped": skipped}

    inserted = unchanged = 0
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT MAX(CAST(substr(event_key,17) AS INTEGER)) "
            "FROM refill_events WHERE source=? AND event_key LIKE 'thingspeak:fuel:%'",
            (REFILL_SOURCE,),
        ).fetchone()
        max_entry_id = int(row[0] or 0)
        for event_key, occurred_at, source, payload_json, digest, entry_id in prepared:
            if entry_id <= max_entry_id:
                unchanged += 1
                continue
            cursor = db.execute(
                "INSERT OR IGNORE INTO refill_events("
                "event_key,occurred_at,source,payload_json,payload_sha256"
                ") VALUES(?,?,?,?,?)",
                (event_key, occurred_at, source, payload_json, digest),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                unchanged += 1
    return {
        "status": "persisted",
        "inserted": inserted,
        "unchanged": unchanged,
        "skipped": skipped,
        "max_source_entry_id": max((item[-1] for item in prepared), default=max_entry_id),
    }


def persist_weather_forecasts(rows, *, fetched_at: int,
                              database_path=DEFAULT_DB_PATH,
                              timezone_name="Europe/Warsaw") -> dict:
    """Archive changed daily forecasts; identical consecutive payloads are skipped."""
    if type(fetched_at) is not int or fetched_at <= 0:
        raise ValueError("fetched_at must be a positive Unix timestamp")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("forecast rows must be a list")
    zone = ZoneInfo(timezone_name)
    store = _store(database_path)
    prepared = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        try:
            day = date.fromisoformat(str(row.get("day", "")))
        except (TypeError, ValueError):
            skipped += 1
            continue
        temperature = _finite_number(row.get("t_mean"), minimum=-90.0, maximum=70.0)
        if temperature is None:
            skipped += 1
            continue
        solar = _finite_number(row.get("solar_mj"), minimum=0.0)
        wind = _finite_number(row.get("wind_max_kmh"), minimum=0.0, maximum=500.0)
        estimated_kg = _finite_number(row.get("kg"), minimum=0.0, maximum=1000.0)
        payload = {
            "schema": "thermoflow-weather-forecast/1",
            "day": day.isoformat(),
            "temperature_mean_c": round(temperature, 2),
            "solar_mj_m2": round(solar, 3) if solar is not None else None,
            "wind_max_kmh": round(wind, 2) if wind is not None else None,
            "estimated_pellet_kg_day": round(estimated_kg, 2) if estimated_kg is not None else None,
            "estimate_semantics": "fuel_model_prediction_not_measurement",
        }
        payload_json, digest = _canonical_payload(payload)
        forecast_for = int(datetime.combine(day, time.min, tzinfo=zone).timestamp())
        prepared.append((forecast_for, payload_json, digest))

    if not prepared:
        return {"status": "empty", "inserted": 0, "unchanged": 0, "skipped": skipped}

    inserted = unchanged = 0
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for forecast_for, payload_json, digest in prepared:
            latest = db.execute(
                "SELECT payload_sha256 FROM weather_forecasts "
                "WHERE source=? AND forecast_for=? "
                "ORDER BY fetched_at DESC,id DESC LIMIT 1",
                (WEATHER_SOURCE, forecast_for),
            ).fetchone()
            if latest is not None and latest["payload_sha256"] == digest:
                unchanged += 1
                continue
            cursor = db.execute(
                "INSERT INTO weather_forecasts("
                "source,forecast_for,fetched_at,payload_json,payload_sha256"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(source,forecast_for,fetched_at) DO UPDATE SET "
                "payload_json=excluded.payload_json,"
                "payload_sha256=excluded.payload_sha256",
                (WEATHER_SOURCE, forecast_for, fetched_at, payload_json, digest),
            )
            inserted += 1 if cursor.rowcount else 0
    return {
        "status": "persisted",
        "inserted": inserted,
        "unchanged": unchanged,
        "skipped": skipped,
    }
