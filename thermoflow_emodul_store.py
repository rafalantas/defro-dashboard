"""Fail-soft persistence for connected normalized eModul API snapshots.

This module never creates a missing database, applies migrations, starts a
collector, or performs upstream network I/O. It only writes to an already
initialized shared ThermoFlow SQLite schema.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

SOURCE = "emodul"


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


def persist_emodul_snapshot(payload, *, database_path=None, module_id=None,
                            recorded_at=None):
    """Persist one complete connected snapshot; return inserted/duplicate/conflict.

    A missing database or unapplied schema is a no-op. The API runtime must
    never initialize storage implicitly.
    """
    if not isinstance(payload, dict) or payload.get("connected") is not True:
        return {"status": "skipped", "reason": "source_not_connected"}
    observed_at = _timestamp(payload.get("fetched_at"))
    if observed_at is None:
        return {"status": "skipped", "reason": "invalid_observation_timestamp"}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"status": "skipped", "reason": "snapshot_data_missing"}

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

    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return {"status": "skipped", "reason": "invalid_json_payload"}
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
                "recorded_at": now, "payload_sha256": digest}
    except sqlite3.Error:
        # Archival failures must not affect the existing live polling path.
        return {"status": "storage_error", "source": SOURCE}
