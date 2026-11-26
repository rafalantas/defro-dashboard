"""Read-only Fibaro HC3 history collector backed by the shared ThermoFlow DB.

The caller controls collection cadence and bounded windows. This module never
starts a worker and only invokes the provider's GET-only history method.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from thermoflow_store import SQLiteStore

MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_DAYS = 90
COLLECTOR_NAME = "fibaro_hc3_history"
BACKFILL_COLLECTOR_NAME = "fibaro_hc3_backfill"


def _valid_timestamp(value):
    return type(value) is int and 0 < value <= 253402300799


def _is_relevant_heating_event(event):
    """Keep only valid, changed heating signals; discard unrelated HC3 chatter."""
    if not isinstance(event, dict):
        return False
    data = event.get("data")
    if not isinstance(data, dict):
        return False
    event_type = event.get("type")
    if event_type == "ClimateZoneTemperatureChangedEvent":
        zone_id = data.get("id")
        if zone_id is None:
            zone_id = data.get("zoneId")
        if not _event_positive_id(zone_id):
            return False
        old_value, new_value = data.get("oldValue"), data.get("newValue")
        return (
            _finite_event_number(old_value, 0.0, 40.0)
            and _finite_event_number(new_value, 0.0, 40.0)
            and old_value != new_value
        )
    if event_type != "DevicePropertyUpdatedEvent":
        return False
    property_name = data.get("property")
    if property_name not in {
        "heatingThermostatSetpoint", "heatRequired", "windowOpened"
    } or not _event_positive_id(data.get("id")):
        return False
    old_value, new_value = data.get("oldValue"), data.get("newValue")
    if property_name in {"heatRequired", "windowOpened"}:
        return (
            isinstance(old_value, bool) and isinstance(new_value, bool)
            and old_value != new_value
        )
    return (
        _finite_event_number(old_value, 5.0, 35.0)
        and _finite_event_number(new_value, 5.0, 35.0)
        and old_value != new_value
    )


def _finite_event_number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and minimum <= value <= maximum


def _event_positive_id(value):
    return type(value) is int and value > 0

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

    def save_batch(self, events, *, start, end, collected_at=None, collector_name=COLLECTOR_NAME):
        self._validate_window(start, end)
        if not isinstance(collector_name, str) or not collector_name.strip():
            raise ValueError("collector_name must be a non-empty string")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        now = int(time.time()) if collected_at is None else collected_at
        if not _valid_timestamp(now):
            raise ValueError("collected_at must be a positive UTC timestamp")

        inserted = duplicates = invalid = conflicts = ignored = 0
        prepared = []
        for event in events:
            identity = _event_identity(event)
            if identity is None or not start <= identity[1] <= end:
                invalid += 1
                continue
            if not _is_relevant_heating_event(event):
                ignored += 1
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
                    collector_name, now, now, start, end, len(events),
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
                                 COALESCE(CAST(collector_state.cursor_value AS INTEGER),0)
                            THEN excluded.cursor_value
                            ELSE collector_state.cursor_value
                        END,
                        updated_at = excluded.updated_at""",
                    (collector_name, str(end), now, json.dumps({
                        "last_successful_at": now,
                        "last_window_start": start,
                        "last_window_end": end,
                        "last_inserted": inserted,
                        "last_duplicates": duplicates,
                        "consecutive_failures": 0,
                    }, separators=(",", ":"))),
                )

        return {
            "fetched": len(events),
            "inserted": inserted,
            "duplicates": duplicates,
            "invalid": invalid,
            "conflicts": conflicts,
            "ignored_irrelevant": ignored,
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
        # HC3 changes fetched_at on every response; it is observation
        # metadata, not a zone/head state transition. Keep it in the stored
        # payload but exclude it from the snapshot content identity.
        fingerprint = dict(snapshot)
        fingerprint.pop("fetched_at", None)
        fingerprint_payload = json.dumps(
            fingerprint, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        digest = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
        metadata = json.dumps(
            {"payload_sha256": digest}, sort_keys=True, separators=(",", ":")
        )
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                "SELECT metadata_json FROM collector_state "
                "WHERE collector_name='fibaro_hc3_snapshot'"
            ).fetchone()
            latest_digest = None
            if state is not None:
                try:
                    latest_digest = json.loads(state["metadata_json"] or "{}").get("payload_sha256")
                except (TypeError, ValueError):
                    latest_digest = None
            if latest_digest is None:
                latest = db.execute(
                    "SELECT payload_sha256 FROM fibaro_snapshots "
                    "ORDER BY captured_at DESC,id DESC LIMIT 1"
                ).fetchone()
                latest_digest = latest["payload_sha256"] if latest is not None else None

            if latest_digest == digest:
                existing = db.execute(
                    "SELECT id,captured_at FROM fibaro_snapshots "
                    "WHERE payload_sha256=? ORDER BY captured_at DESC,id DESC LIMIT 1",
                    (digest,),
                ).fetchone()
            else:
                existing = None

            if existing is not None:
                snapshot_id = int(existing["id"])
                status = "duplicate"
                original_captured_at = int(existing["captured_at"])
            else:
                # Record every content transition, including A -> B -> A.
                # Global content deduplication would erase the second A's
                # effective timestamp and corrupt historical zone mapping.
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
            state = db.execute(
                "SELECT updated_at,metadata_json FROM collector_state "
                "WHERE collector_name='fibaro_hc3_snapshot'"
            ).fetchone()
            latest_digest = None
            if state is not None:
                try:
                    metadata = json.loads(state["metadata_json"] or "{}")
                    latest_digest = metadata.get("payload_sha256")
                except (TypeError, ValueError):
                    latest_digest = None
            if isinstance(latest_digest, str) and len(latest_digest) == 64:
                row = db.execute(
                    "SELECT id,captured_at,payload_json,payload_sha256 "
                    "FROM fibaro_snapshots WHERE payload_sha256=? "
                    "ORDER BY captured_at DESC,id DESC LIMIT 1",
                    (latest_digest,),
                ).fetchone()
            else:
                # Backward-compatible fallback for databases written before
                # the latest-observation digest was stored in collector_state.
                row = db.execute(
                    "SELECT id,captured_at,payload_json,payload_sha256 "
                    "FROM fibaro_snapshots ORDER BY captured_at DESC,id DESC LIMIT 1"
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

    def read_snapshots(self, *, start=None, end=None, limit=10000):
        """Read ordered snapshot evidence, including the last pre-window snapshot."""
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("limit must be between 1 and 100000")
        if start is not None and not _valid_timestamp(start):
            raise ValueError("invalid start")
        if end is not None and not _valid_timestamp(end):
            raise ValueError("invalid end")
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be after end")
        with self.store.connection() as db:
            prior = None
            if start is not None:
                prior = db.execute(
                    "SELECT id,captured_at,payload_json,payload_sha256 FROM fibaro_snapshots "
                    "WHERE captured_at < ? ORDER BY captured_at DESC,id DESC LIMIT 1",
                    (start,),
                ).fetchone()
            rows = db.execute(
                "SELECT id,captured_at,payload_json,payload_sha256 FROM fibaro_snapshots "
                "WHERE captured_at >= ? AND captured_at <= COALESCE(?,253402300799) "
                "ORDER BY captured_at,id LIMIT ?",
                (start if start is not None else 1, end, limit),
            ).fetchall()
            if prior is not None:
                rows = [prior, *rows]
        return [{
            "id": int(row["id"]),
            "captured_at": int(row["captured_at"]),
            "payload": json.loads(row["payload_json"]),
            "payload_sha256": row["payload_sha256"],
        } for row in rows[:limit]]

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

    def status(self, collector_name=COLLECTOR_NAME):
        if not isinstance(collector_name, str) or not collector_name.strip():
            raise ValueError("collector_name must be a non-empty string")
        with self.store.connection() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM fibaro_events").fetchone()["n"]
            latest = db.execute("SELECT MAX(observed_at) AS n FROM fibaro_events").fetchone()["n"]
            cursor = db.execute(
                "SELECT cursor_value,updated_at,metadata_json FROM collector_state WHERE collector_name=?",
                (collector_name,),
            ).fetchone()
            run = db.execute(
                "SELECT * FROM collection_runs WHERE collector_name=? "
                "ORDER BY id DESC LIMIT 1",
                (collector_name,),
            ).fetchone()
        try:
            metadata = json.loads(cursor["metadata_json"] or "{}") if cursor else {}
        except (TypeError, ValueError):
            metadata = {}
        return {
            "schema": "thermoflow-fibaro-history/3",
            "collector_name": collector_name,
            "events": count,
            "latest_event_timestamp": latest,
            "last_successful_end": int(cursor["cursor_value"]) if cursor and cursor["cursor_value"] else None,
            "last_updated_at": int(cursor["updated_at"]) if cursor else None,
            "metadata": metadata,
            "backfill_complete": bool(metadata.get("backfill_complete")) if collector_name == BACKFILL_COLLECTOR_NAME else None,
            "last_run": dict(run) if run else None,
            "persistent": self.store.path != ":memory:",
            "shared_store": True,
            "read_only_source": True,
        }


    def event_coverage(self, *, start, end):
        """Return exact stored event coverage without loading raw payloads into memory."""
        if (not _valid_timestamp(start) or not _valid_timestamp(end)
                or start >= end):
            raise ValueError("event coverage requires an ordered timestamp range")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n,MIN(observed_at) AS first_at,MAX(observed_at) AS last_at "
                "FROM fibaro_events WHERE observed_at >= ? AND observed_at <= ?",
                (start, end),
            ).fetchone()
        return {
            "stored_events": int(row["n"]),
            "stored_events_in_window": int(row["n"]),
            "first_event_timestamp": int(row["first_at"]) if row["first_at"] is not None else None,
            "last_event_timestamp": int(row["last_at"]) if row["last_at"] is not None else None,
        }

    def record_failure(self, *, collector_name=COLLECTOR_NAME, error_type="CollectionError",
                       attempted_at=None, window_start=None, window_end=None):
        """Persist a sanitized failed attempt without advancing its successful cursor."""
        stamp = int(time.time()) if attempted_at is None else attempted_at
        if not _valid_timestamp(stamp):
            raise ValueError("attempted_at must be a positive UTC timestamp")
        if not isinstance(collector_name, str) or not collector_name.strip():
            raise ValueError("collector_name must be a non-empty string")
        safe_type = str(error_type)[:80]
        if not safe_type.replace("_", "").isalnum():
            safe_type = "CollectionError"
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata_json FROM collector_state WHERE collector_name=?",
                (collector_name,),
            ).fetchone()
            try:
                metadata = json.loads(row["metadata_json"] or "{}") if row else {}
            except (TypeError, ValueError):
                metadata = {}
            failures = int(metadata.get("consecutive_failures", 0) or 0) + 1
            metadata.update({
                "last_attempt_at": stamp,
                "last_error_type": safe_type,
                "consecutive_failures": failures,
            })
            db.execute(
                """INSERT INTO collection_runs(
                    collector_name,started_at,completed_at,window_start,window_end,
                    fetched,inserted,duplicates,invalid,status,details_json
                ) VALUES(?,?,?,?,?,0,0,0,0,'error',?)""",
                (collector_name, stamp, stamp, window_start, window_end,
                 json.dumps({"error_type": safe_type}, separators=(",", ":"))),
            )
            db.execute(
                """INSERT INTO collector_state(collector_name,cursor_value,updated_at,metadata_json)
                   VALUES(?,NULL,?,?)
                   ON CONFLICT(collector_name) DO UPDATE SET
                     updated_at=excluded.updated_at,
                     metadata_json=excluded.metadata_json""",
                (collector_name, stamp, json.dumps(metadata, separators=(",", ":"))),
            )
        return {"status": "error_recorded", "error_type": safe_type,
                "consecutive_failures": failures, "cursor_advanced": False}

    def mark_cursor(self, cursor_value, *, collector_name=COLLECTOR_NAME,
                    recorded_at=None, metadata=None):
        """Advance a cursor only after a caller has verified a complete source range."""
        if not _valid_timestamp(cursor_value):
            raise ValueError("cursor_value must be a positive UTC timestamp")
        stamp = int(time.time()) if recorded_at is None else recorded_at
        if not _valid_timestamp(stamp):
            raise ValueError("recorded_at must be a positive UTC timestamp")
        if not isinstance(collector_name, str) or not collector_name.strip():
            raise ValueError("collector_name must be a non-empty string")
        details = dict(metadata or {})
        details.setdefault("last_successful_at", stamp)
        details.setdefault("consecutive_failures", 0)
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO collector_state(collector_name,cursor_value,updated_at,metadata_json)
                   VALUES(?,?,?,?)
                   ON CONFLICT(collector_name) DO UPDATE SET
                     cursor_value=CASE
                       WHEN CAST(excluded.cursor_value AS INTEGER) > COALESCE(CAST(collector_state.cursor_value AS INTEGER),0)
                       THEN excluded.cursor_value ELSE collector_state.cursor_value END,
                     updated_at=excluded.updated_at,
                     metadata_json=excluded.metadata_json""",
                (collector_name, str(cursor_value), stamp,
                 json.dumps(details, sort_keys=True, separators=(",", ":"))),
            )
        return {"collector_name": collector_name, "cursor_value": cursor_value}

    def set_backfill_initial_start(self, initial_start, *, recorded_at=None):
        """Persist the first chosen backfill boundary so restarts cannot move it."""
        if not _valid_timestamp(initial_start):
            raise ValueError("initial_start must be a positive UTC timestamp")
        stamp = int(time.time()) if recorded_at is None else recorded_at
        if not _valid_timestamp(stamp):
            raise ValueError("recorded_at must be a positive UTC timestamp")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata_json FROM collector_state WHERE collector_name=?",
                (BACKFILL_COLLECTOR_NAME,),
            ).fetchone()
            try:
                metadata = json.loads(row["metadata_json"] or "{}") if row else {}
            except (TypeError, ValueError):
                metadata = {}
            existing = metadata.get("initial_start")
            if type(existing) is int and existing > 0:
                return existing
            metadata["initial_start"] = initial_start
            db.execute(
                """INSERT INTO collector_state(
                    collector_name,cursor_value,updated_at,metadata_json
                ) VALUES(?,NULL,?,?)
                ON CONFLICT(collector_name) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json""",
                (BACKFILL_COLLECTOR_NAME, stamp,
                 json.dumps(metadata, sort_keys=True, separators=(",", ":"))),
            )
        return initial_start

    def mark_backfill_complete(self, cursor_value, *, recorded_at=None, initial_start=None):
        """Persist the one-time backfill completion marker in the existing state table."""
        completed_at = int(time.time()) if recorded_at is None else recorded_at
        metadata = {
            "backfill_complete": True,
            "backfill_start": initial_start,
            "backfill_completed_at": completed_at,
            "consecutive_failures": 0,
        }
        return self.mark_cursor(
            cursor_value, collector_name=BACKFILL_COLLECTOR_NAME,
            recorded_at=recorded_at, metadata=metadata,
        )

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
