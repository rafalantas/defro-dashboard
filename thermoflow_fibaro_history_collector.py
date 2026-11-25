"""Durable, idempotent local collector for read-only Fibaro HC3 event history.

The collector is deliberately pull-driven: callers choose a bounded time window.
It only calls the provider's GET-only history method and writes to a local SQLite
database. It never sends commands to HC3 and never starts its own thread.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_DAYS = 90
SCHEMA_VERSION = 1


def _valid_timestamp(value):
    return type(value) is int and 0 < value <= 253402300799


def _event_identity(event):
    if not isinstance(event, dict):
        return None
    event_id = event.get("id")
    timestamp = event.get("timestamp")
    if type(event_id) is not int or event_id <= 0 or not _valid_timestamp(timestamp):
        return None
    return event_id, timestamp


class FibaroHistoryStore:
    """SQLite event ledger with unique HC3 event IDs and atomic cursor updates."""

    def __init__(self, path=None, *, retention_days=DEFAULT_RETENTION_DAYS):
        self.path = str(path or os.getenv(
            "FIBARO_HISTORY_DB_PATH", "/var/lib/thermoflow/fibaro/history.sqlite3"
        ))
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        self.retention_days = retention_days
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY,
                timestamp INTEGER NOT NULL,
                event_type TEXT,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                collected_at INTEGER NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_fibaro_events_timestamp ON events(timestamp)")
            db.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                fetched INTEGER NOT NULL,
                inserted INTEGER NOT NULL,
                duplicates INTEGER NOT NULL,
                invalid INTEGER NOT NULL,
                completed_at INTEGER NOT NULL,
                status TEXT NOT NULL
            )""")
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)",
                       (str(SCHEMA_VERSION),))
            row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if row is None or row["value"] != str(SCHEMA_VERSION):
                raise RuntimeError("unsupported Fibaro history database schema")

    def save_batch(self, events, *, start, end, collected_at=None):
        if not _valid_timestamp(start) or not _valid_timestamp(end) or start >= end or end - start > MAX_WINDOW_SECONDS:
            raise ValueError("collection window must be ordered and no longer than 7 days")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        now = int(time.time()) if collected_at is None else collected_at
        if not _valid_timestamp(now):
            raise ValueError("collected_at must be a positive UTC timestamp")

        inserted = duplicates = invalid = 0
        prepared = []
        for event in events:
            identity = _event_identity(event)
            if identity is None or not start <= identity[1] <= end:
                invalid += 1
                continue
            event_id, timestamp = identity
            try:
                payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                invalid += 1
                continue
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            event_type = event.get("type") if isinstance(event.get("type"), str) else None
            prepared.append((event_id, timestamp, event_type, payload, digest, now))

        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in prepared:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO events(event_id,timestamp,event_type,payload_json,payload_sha256,collected_at) VALUES(?,?,?,?,?,?)",
                    row,
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
            status = "complete" if invalid == 0 else "partial"
            db.execute(
                "INSERT INTO collection_runs(start_ts,end_ts,fetched,inserted,duplicates,invalid,completed_at,status) VALUES(?,?,?,?,?,?,?,?)",
                (start, end, len(events), inserted, duplicates, invalid, now, status),
            )
            if status == "complete":
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES('last_successful_end',?) ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))",
                    (str(end),),
                )
        return {"fetched": len(events), "inserted": inserted, "duplicates": duplicates,
                "invalid": invalid, "status": status, "cursor_end": end}

    def collect_once(self, provider, *, start, end, collected_at=None):
        """Fetch one explicitly requested window using the provider's GET-only API."""
        events = provider.get_events_range(start, end)
        return self.save_batch(events, start=start, end=end, collected_at=collected_at)

    def read_events(self, *, start=None, end=None, limit=10000):
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("limit must be between 1 and 100000")
        clauses, values = [], []
        if start is not None:
            if not _valid_timestamp(start):
                raise ValueError("invalid start")
            clauses.append("timestamp >= ?")
            values.append(start)
        if end is not None:
            if not _valid_timestamp(end):
                raise ValueError("invalid end")
            clauses.append("timestamp <= ?")
            values.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as db:
            rows = db.execute(
                "SELECT payload_json FROM events" + where + " ORDER BY timestamp,event_id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def status(self):
        with self._connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            latest = db.execute("SELECT MAX(timestamp) AS n FROM events").fetchone()["n"]
            cursor = db.execute("SELECT value FROM metadata WHERE key='last_successful_end'").fetchone()
            run = db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"schema": "thermoflow-fibaro-history-store/1", "events": count,
                "latest_event_timestamp": latest,
                "last_successful_end": int(cursor["value"]) if cursor else None,
                "last_run": dict(run) if run else None, "persistent": self.path != ":memory:",
                "read_only_source": True}

    def prune(self, *, now=None):
        clock = int(time.time()) if now is None else now
        if not _valid_timestamp(clock):
            raise ValueError("invalid now")
        cutoff = clock - self.retention_days * 86400
        with self._connection() as db:
            cursor = db.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
            deleted = cursor.rowcount
        return {"deleted": deleted, "cutoff": cutoff, "retention_days": self.retention_days}
