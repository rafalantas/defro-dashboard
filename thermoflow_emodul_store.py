"""Curated eModul telemetry persistence for ThermoFlow AI/ML.

Only stable measurement and operating-context fields are stored. UI-only
diagnostics (tiles, zones, menus, translations and statistics) are excluded.
No migrations, collectors, network calls or device commands are performed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

SOURCE = "emodul"
SAMPLE_SCHEMA = "thermoflow-emodul-ml-input/1"

# Explicit allow-list: fields useful for time-series diagnostics and ML.
# Deliberately excludes raw tile/menu/zone structures and UI presentation data.
FIELD_ALLOWLIST = {
    "temperatures": {"co", "dhw", "flue", "outdoor", "room", "return", "buffer_upper"},
    "boiler_power_kw": None,
    "boiler_power_at": None,
    "pumps": {"floor", "valve4d", "dhw", "circulation"},
    "valve": {"current", "return", "set", "opening", "set_correction",
              "room_reduction", "set_after_correction", "working"},
    "fuel": {"percent", "hours"},
    "controller_metrics": {"feeder_temperature_c", "instant_fuel_consumption"},
    "fire": None,
    "fire_signal": None,
    "fire_brightness": None,
    "fan": {"running", "percent", "rpm"},
    "outputs": {"feeder", "heater"},
    "disinfection": {"active", "percent"},
    "mode": None,
    "controller_status": None,
    "setpoints": {"co", "dhw", "room"},
    "hysteresis": {"boiler", "dhw", "valve"},
}


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        stamp = int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return stamp if stamp > 0 else None


def _valid_value(value):
    # Missing measurements are not observations: omit them instead of
    # serializing dozens of null fields into every poll sample.
    if value is None:
        return False
    if isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def select_ml_fields(data):
    """Return only allow-listed scalar measurements, preserving useful groups."""
    if not isinstance(data, dict):
        return {}
    selected = {}
    for field, members in FIELD_ALLOWLIST.items():
        value = data.get(field)
        if members is None:
            if _valid_value(value):
                selected[field] = value
            continue
        if not isinstance(value, dict):
            continue
        group = {
            key: item for key, item in value.items()
            if key in members and _valid_value(item)
        }
        if group:
            selected[field] = group
    return selected


def persist_emodul_snapshot(payload, *, database_path=None, module_id=None,
                            recorded_at=None):
    """Persist one curated ML input sample; return inserted/duplicate/conflict.

    A missing database or unapplied schema is a no-op. The API runtime must
    never initialize storage implicitly.
    """
    if not isinstance(payload, dict) or payload.get("connected") is not True:
        return {"status": "skipped", "reason": "source_not_connected"}
    observed_at = _timestamp(payload.get("fetched_at"))
    if observed_at is None:
        return {"status": "skipped", "reason": "invalid_observation_timestamp"}
    selected = select_ml_fields(payload.get("data"))
    if not selected:
        return {"status": "skipped", "reason": "no_ml_fields"}

    path = str(database_path or os.getenv("THERMOFLOW_DB_PATH", DEFAULT_DB_PATH))
    if not Path(path).expanduser().is_file():
        return {"status": "skipped", "reason": "database_missing"}
    module = str(module_id or "default").strip()
    if not module or len(module) > 200:
        return {"status": "skipped", "reason": "invalid_module_id"}
    fetched_at = payload["fetched_at"].strip()
    source_key = f"{module}:{fetched_at}"
    now = int(time.time()) if recorded_at is None else recorded_at
    if type(now) is not int or now <= 0:
        return {"status": "skipped", "reason": "invalid_recorded_at"}

    sample = {
        "schema": SAMPLE_SCHEMA,
        "source": SOURCE,
        "module_id": module,
        "observed_at": observed_at,
        "data": selected,
    }
    try:
        encoded = json.dumps(sample, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "invalid_ml_sample"}
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    store = SQLiteStore(path)
    try:
        with store.connection() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry_samples'"
            ).fetchone()
            if table is None:
                return {"status": "skipped", "reason": "schema_not_ready"}
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT observed_at,payload_sha256 FROM telemetry_samples "
                "WHERE source=? AND source_key=?",
                (SOURCE, source_key),
            ).fetchone()
            if existing is not None:
                if (int(existing["observed_at"]) == observed_at
                        and existing["payload_sha256"] == digest):
                    return {"status": "duplicate", "source": SOURCE,
                            "source_key": source_key, "observed_at": observed_at}
                return {"status": "conflict", "source": SOURCE,
                        "source_key": source_key, "observed_at": observed_at}
            db.execute(
                """INSERT INTO telemetry_samples(
                    source,source_key,observed_at,recorded_at,payload_json,payload_sha256
                ) VALUES(?,?,?,?,?,?)""",
                (SOURCE, source_key, observed_at, now, encoded, digest),
            )
        return {"status": "inserted", "source": SOURCE,
                "source_key": source_key, "observed_at": observed_at,
                "recorded_at": now, "payload_sha256": digest,
                "stored_fields": sorted(selected)}
    except sqlite3.Error:
        # Archival failures must not affect the existing live polling path.
        return {"status": "storage_error", "source": SOURCE}
