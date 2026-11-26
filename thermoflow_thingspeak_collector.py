"""Explicit, bounded ThingSpeak ingestion into the shared ThermoFlow SQLite store.

The caller supplies the GET-only fetch function and channel configuration.
This module never reads credentials, performs network I/O at import time,
starts a scheduler, or changes the existing fuel/API read paths.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from thermoflow_store import SQLiteStore

MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
MAX_FEEDS_PER_CHANNEL = 8000
MAX_BATCH_FEEDS_PER_CHANNEL = 50000
COLLECTOR_NAME = "thingspeak_history"


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        stamp = int(parsed.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return stamp if 0 < stamp <= 253402300799 else None


def _entry_id(feed):
    value = feed.get("entry_id") if isinstance(feed, dict) else None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _payload(feed):
    if not isinstance(feed, dict):
        return None
    # Identity and timestamp already have indexed relational columns.
    compact = {key: value for key, value in feed.items()
               if key not in {"entry_id", "created_at"}}
    try:
        value = json.dumps(
            compact, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    return value, hashlib.sha256(value.encode("utf-8")).hexdigest()

def _is_duplicate(existing, digest, observed_at, compact_payload_json):
    """Recognize both new compact rows and legacy full-feed rows during rollout."""
    if existing["observed_at"] != observed_at:
        return False
    if existing["payload_sha256"] == digest:
        return True
    try:
        legacy = json.loads(existing["payload_json"])
        if not isinstance(legacy, dict) or not {"entry_id", "created_at"}.issubset(legacy):
            return False
        legacy.pop("entry_id", None)
        legacy.pop("created_at", None)
        normalized = json.dumps(
            legacy, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return False
    return normalized == compact_payload_json


class ThingSpeakHistoryCollector:
    """Idempotent ingestion for compact telemetry and refill event channels."""

    def __init__(self, store: SQLiteStore | None = None):
        self.store = store or SQLiteStore()
        self._require_schema()

    def _require_schema(self):
        required = {
            "telemetry_samples", "refill_events",
            "collector_state", "collection_runs",
        }
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
            type(start) is not int or type(end) is not int
            or start <= 0 or end <= start
            or end > 253402300799
            or end - start > MAX_WINDOW_SECONDS
        ):
            raise ValueError("ThingSpeak window must be ordered and no longer than 7 days")

    @staticmethod
    def _validate_channels(channels):
        if not isinstance(channels, dict) or not channels:
            raise ValueError("channels must be a non-empty mapping")
        for name, config in channels.items():
            if (
                not isinstance(name, str) or not name
                or not isinstance(config, dict)
                or config.get("kind") not in {"telemetry", "refills"}
                or not isinstance(config.get("channel_id"), (str, int))
                or not str(config["channel_id"]).strip()
            ):
                raise ValueError("each channel needs a name, channel_id and supported kind")

    def save_batch(self, channels, *, start, end, collected_at=None):
        """Persist already-fetched channel feeds atomically; no upstream writes."""
        self._validate_window(start, end)
        self._validate_channels(channels)
        now = int(datetime.now(timezone.utc).timestamp()) if collected_at is None else collected_at
        if type(now) is not int or not 0 < now <= 253402300799:
            raise ValueError("collected_at must be a positive UTC timestamp")

        prepared = []
        fetched = inserted = duplicates = invalid = conflicts = 0
        for name, config in channels.items():
            feeds = config.get("feeds")
            if not isinstance(feeds, list) or len(feeds) > MAX_BATCH_FEEDS_PER_CHANNEL:
                raise ValueError("feeds must be a list of at most 50000 records")
            channel_id = str(config["channel_id"])
            kind = config["kind"]
            fetched += len(feeds)
            for feed in feeds:
                entry_id = _entry_id(feed)
                observed_at = _timestamp(feed.get("created_at")) if isinstance(feed, dict) else None
                encoded = _payload(feed)
                if (
                    entry_id is None or observed_at is None
                    or not start <= observed_at <= end or encoded is None
                ):
                    invalid += 1
                    continue
                payload_json, digest = encoded
                prepared.append((
                    name, channel_id, kind, entry_id, observed_at,
                    payload_json, digest,
                ))

        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for name, channel_id, kind, entry_id, observed_at, payload_json, digest in prepared:
                if kind == "telemetry":
                    source = f"thingspeak:{name}"
                    source_key = f"{channel_id}:{entry_id}"
                    existing = db.execute(
                        "SELECT payload_sha256,observed_at,payload_json FROM telemetry_samples "
                        "WHERE source=? AND source_key=?",
                        (source, source_key),
                    ).fetchone()
                    if existing is not None:
                        if _is_duplicate(existing, digest, observed_at, payload_json):
                            duplicates += 1
                        else:
                            conflicts += 1
                            invalid += 1
                        continue
                    db.execute(
                        """INSERT INTO telemetry_samples(
                            source, source_key, observed_at, recorded_at,
                            payload_json, payload_sha256
                        ) VALUES(?,?,?,?,?,?)""",
                        (source, source_key, observed_at, now, payload_json, digest),
                    )
                else:
                    source = f"thingspeak:{name}"
                    event_key = f"{channel_id}:{entry_id}"
                    existing = db.execute(
                        "SELECT payload_sha256,occurred_at AS observed_at,payload_json FROM refill_events WHERE event_key=?",
                        (event_key,),
                    ).fetchone()
                    if existing is not None:
                        if _is_duplicate(existing, digest, observed_at, payload_json):
                            duplicates += 1
                        else:
                            conflicts += 1
                            invalid += 1
                        continue
                    db.execute(
                        """INSERT INTO refill_events(
                            event_key, occurred_at, source, payload_json, payload_sha256
                        ) VALUES(?,?,?,?,?)""",
                        (event_key, observed_at, source, payload_json, digest),
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
                    COLLECTOR_NAME, now, now, start, end, fetched,
                    inserted, duplicates, invalid, status,
                    json.dumps({
                        "conflicts": conflicts,
                        "channels": {
                            name: {"kind": config["kind"], "channel_id": str(config["channel_id"])}
                            for name, config in channels.items()
                        },
                    }, sort_keys=True, separators=(",", ":")),
                ),
            )

            # A partial batch never advances any channel cursor. Cursors are
            # monotonic and updated in the same transaction as the stored rows.
            if status == "complete":
                for name in channels:
                    collector_name = f"{COLLECTOR_NAME}:{name}"
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
                        (collector_name, str(end), now, "{}"),
                    )

        return {
            "fetched": fetched, "inserted": inserted, "duplicates": duplicates,
            "invalid": invalid, "conflicts": conflicts, "status": status,
            "window_start": start, "window_end": end,
        }

    def collect_once(self, fetch_channel, channels, *, start, end, collected_at=None):
        """Fetch one bounded window per configured channel using caller's GET-only adapter."""
        self._validate_window(start, end)
        self._validate_channels(channels)
        fetched_channels = {}
        for name, config in channels.items():
            feeds = fetch_channel(str(config["channel_id"]), start, end)
            fetched_channels[name] = {**config, "feeds": feeds}
        return self.save_batch(
            fetched_channels, start=start, end=end, collected_at=collected_at
        )

    def status(self):
        with self.store.connection() as db:
            counts = {
                "telemetry_samples": db.execute(
                    "SELECT COUNT(*) FROM telemetry_samples WHERE source LIKE 'thingspeak:%'"
                ).fetchone()[0],
                "refill_events": db.execute(
                    "SELECT COUNT(*) FROM refill_events WHERE source LIKE 'thingspeak:%'"
                ).fetchone()[0],
            }
            runs = db.execute(
                "SELECT * FROM collection_runs WHERE collector_name=? ORDER BY id DESC LIMIT 1",
                (COLLECTOR_NAME,),
            ).fetchone()
            cursors = {
                row["collector_name"]: row["cursor_value"]
                for row in db.execute(
                    "SELECT collector_name,cursor_value FROM collector_state "
                    "WHERE collector_name LIKE 'thingspeak_history:%'"
                )
            }
        return {
            "schema": "thermoflow-thingspeak-history/1",
            "counts": counts,
            "cursors": cursors,
            "last_run": dict(runs) if runs else None,
            "shared_store": True,
            "read_only_source": True,
        }
