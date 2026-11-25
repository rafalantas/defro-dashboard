"""Read-only Fibaro HC3 history collector backed by the shared ThermoFlow DB.

The caller controls collection cadence and bounded windows. This module never
starts a worker and only invokes the provider's GET-only history method.
"""
from __future__ import annotations

import hashlib
import json
import time

from thermoflow_store import SQLiteStore

MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_DAYS = 90
COLLECTOR_NAME = "fibaro_hc3_history"


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


class FibaroHistoryCollector:
    """Idempotent Fibaro history ingestion into the shared SQLite schema."""

    def __init__(self, store: SQLiteStore | None = None, *, retention_days=DEFAULT_RETENTION_DAYS):
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        self.store = store or SQLiteStore()
        self.retention_days = retention_days
        self._require_schema()

    def _require_schema(self):
        required = {"fibaro_events", "collector_state", "collection_runs"}
        with self.store.connection() as db:
            present = {
                row["name"] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        missing = sorted(required - present)
        if missing:
            raise RuntimeError(
                "ThermoFlow database migrations are not applied; missing tables: "
                + ", ".join(missing)
            )

    @staticmethod
    def _validate_window(start, end):
        if (
            not _valid_timestamp(start)
            or not _valid_timestamp(end)
            or start >= end
            or end - start > MAX_WINDOW_SECONDS
        ):
            raise ValueError("collection window must be ordered and no longer than 7 days")

    def save_batch(self, events, *, start, end, collected_at=None):
        self._validate_window(start, end)
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        now = int(time.time()) if collected_at is None else collected_at
        if not _valid_timestamp(now):
            raise ValueError("collected_at must be a positive UTC timestamp")

        inserted = duplicates = invalid = conflicts = 0
        prepared = []
        for event in events:
            identity = _event_identity(event)
            if identity is None or not start <= identity[1] <= end:
                invalid += 1
                continue
            event_id, timestamp = identity
            try:
                payload = json.dumps(
                    event, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )
            except (TypeError, ValueError):
                invalid += 1
                continue
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            event_type = event.get("type") if isinstance(event.get("type"), str) else None
            prepared.append((event_id, timestamp, event_type, payload, digest, now))

        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in prepared:
                event_id, timestamp, event_type, payload, digest, recorded_at = row
                existing = db.execute(
                    "SELECT payload_sha256 FROM fibaro_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] == digest:
                        duplicates += 1
                    else:
                        # An HC3 event ID must be immutable. Never overwrite the
                        # original event if the source later reuses that ID.
                        conflicts += 1
                        invalid += 1
                    continue
                db.execute(
                    """INSERT INTO fibaro_events(
                        event_id, observed_at, event_type, payload_json,
                        payload_sha256, collected_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (event_id, timestamp, event_type, payload, digest, recorded_at),
                )
                inserted += 1

            status = "complete" if invalid == 0 else "partial"
            db.execute(
                """INSERT INTO collection_runs(
                    collector_name, started_at, completed_at, window_start,
                    window_end, fetched, inserted, duplicates, invalid,
                    status, details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    COLLECTOR_NAME, now, now, start, end, len(events),
                    inserted, duplicates, invalid, status,
                    json.dumps({"conflicts": conflicts}, separators=(",", ":")),
                ),
            )
            if status == "complete":
                db.execute(
                    """INSERT INTO collector_state(
                        collector_name, cursor_value, updated_at, metadata_json
                    ) VALUES(?,?,?,?)
                    ON CONFLICT(collector_name) DO UPDATE SET
                        cursor_value = CASE
                            WHEN CAST(excluded.cursor_value AS INTEGER) >
                                 CAST(collector_state.cursor_value AS INTEGER)
                            THEN excluded.cursor_value
                            ELSE collector_state.cursor_value
                        END,
                        updated_at = excluded.updated_at""",
                    (COLLECTOR_NAME, str(end), now, "{}"),
                )

        return {
            "fetched": len(events),
            "inserted": inserted,
            "duplicates": duplicates,
            "invalid": invalid,
            "conflicts": conflicts,
            "status": status,
            "cursor_end": end,
        }

    def collect_once(self, provider, *, start, end, collected_at=None):
        """Fetch one explicitly requested window through the GET-only HC3 provider."""
        self._validate_window(start, end)
        events = provider.get_events_range(start, end)
        return self.save_batch(events, start=start, end=end, collected_at=collected_at)

    def save_snapshot(self, snapshot, *, captured_at=None):
        """Persist only changed snapshot content; record every successful observation separately."""
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be a mapping")
        stamp = int(time.time()) if captured_at is None else captured_at
        if not _valid_timestamp(stamp):
            raise ValueError("captured_at must be a positive UTC timestamp")
        try:
            payload = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ValueError("snapshot must contain JSON-safe finite values") from None
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        metadata = json.dumps(
            {"payload_sha256": digest}, sort_keys=True, separators=(",", ":")
        )
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,captured_at FROM fibaro_snapshots "
                "WHERE payload_sha256=? ORDER BY captured_at DESC,id DESC LIMIT 1",
                (digest,),
            ).fetchone()
            if existing is not None:
                snapshot_id = int(existing["id"])
                status = "duplicate"
                original_captured_at = int(existing["captured_at"])
            else:
                cursor = db.execute(
                    "INSERT INTO fibaro_snapshots(captured_at,payload_json,payload_sha256) "
                    "VALUES(?,?,?)",
                    (stamp, payload, digest),
                )
                snapshot_id = int(cursor.lastrowid)
                status = "inserted"
                original_captured_at = stamp

            db.execute(
                """INSERT INTO collector_state(
                    collector_name,cursor_value,updated_at,metadata_json
                ) VALUES(?,?,?,?)
                ON CONFLICT(collector_name) DO UPDATE SET
                    cursor_value = CASE
                        WHEN CAST(excluded.cursor_value AS INTEGER) >
                             CAST(collector_state.cursor_value AS INTEGER)
                        THEN excluded.cursor_value
                        ELSE collector_state.cursor_value
                    END,
                    updated_at = CASE
                        WHEN excluded.updated_at > collector_state.updated_at
                        THEN excluded.updated_at
                        ELSE collector_state.updated_at
                    END,
                    metadata_json = CASE
                        WHEN excluded.updated_at >= collector_state.updated_at
                        THEN excluded.metadata_json
                        ELSE collector_state.metadata_json
                    END""",
                ("fibaro_hc3_snapshot", str(stamp), stamp, metadata),
            )
            return {
                "status": status,
                "id": snapshot_id,
                "captured_at": original_captured_at,
                "last_observed_at": stamp,
                "payload_sha256": digest,
            }

    def read_latest_snapshot(self):
        """Return the latest distinct snapshot and its last observation time."""
        with self.store.connection() as db:
            row = db.execute(
                "SELECT id,captured_at,payload_json,payload_sha256 "
                "FROM fibaro_snapshots ORDER BY captured_at DESC,id DESC LIMIT 1"
            ).fetchone()
            state = db.execute(
                "SELECT updated_at FROM collector_state "
                "WHERE collector_name='fibaro_hc3_snapshot'"
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "captured_at": int(row["captured_at"]),
            "last_observed_at": int(state["updated_at"]) if state else int(row["captured_at"]),
            "payload": json.loads(row["payload_json"]),
            "payload_sha256": row["payload_sha256"],
        }

    def read_events(self, *, start=None, end=None, limit=10000):
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("limit must be between 1 and 100000")
        clauses, values = [], []
        if start is not None:
            if not _valid_timestamp(start):
                raise ValueError("invalid start")
            clauses.append("observed_at >= ?")
            values.append(start)
        if end is not None:
            if not _valid_timestamp(end):
                raise ValueError("invalid end")
            clauses.append("observed_at <= ?")
            values.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT payload_json FROM fibaro_events" + where
                + " ORDER BY observed_at,event_id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def status(self):
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM fibaro_events").fetchone()["n"]
            latest = db.execute("SELECT MAX(observed_at) AS n FROM fibaro_events").fetchone()["n"]
            cursor = db.execute(
                "SELECT cursor_value FROM collector_state WHERE collector_name=?",
                (COLLECTOR_NAME,),
            ).fetchone()
            run = db.execute(
                "SELECT * FROM collection_runs WHERE collector_name=? "
                "ORDER BY id DESC LIMIT 1",
                (COLLECTOR_NAME,),
            ).fetchone()
        return {
            "schema": "thermoflow-fibaro-history/2",
            "events": count,
            "latest_event_timestamp": latest,
            "last_successful_end": int(cursor["cursor_value"]) if cursor else None,
            "last_run": dict(run) if run else None,
            "persistent": self.store.path != ":memory:",
            "shared_store": True,
            "read_only_source": True,
        }

    def prune(self, *, now=None):
        clock = int(time.time()) if now is None else now
        if not _valid_timestamp(clock):
            raise ValueError("invalid now")
        cutoff = clock - self.retention_days * 86400
        with self.store.connection() as db:
            cursor = db.execute(
                "DELETE FROM fibaro_events WHERE observed_at < ?", (cutoff,)
            )
            deleted = cursor.rowcount
        return {
            "deleted": deleted,
            "cutoff": cutoff,
            "retention_days": self.retention_days,
        }
