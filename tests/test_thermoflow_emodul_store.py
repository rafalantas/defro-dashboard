"""Tests for fail-soft normalized eModul snapshot persistence."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from thermoflow_emodul_store import persist_emodul_snapshot
from thermoflow_store import SQLiteStore


class EmodulSnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database)
        self.store.migrate()
        self.payload = {
            "configured": True,
            "connected": True,
            "source": "eModul",
            "fetched_at": "2026-11-26T10:00:00+00:00",
            "controller": {"name": "Defro", "type": "APC"},
            "data": {"temperatures": {"co": 65.0}, "fire": True},
            "tiles": [{"id": 1, "value": 65}],
            "zones": [], "menus": [], "statistics": {},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_complete_normalized_snapshot_with_provenance(self):
        result = persist_emodul_snapshot(
            self.payload, database_path=self.database, module_id="controller-1",
            recorded_at=1795687500,
        )
        self.assertEqual(result["status"], "inserted")
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM telemetry_samples").fetchone()
        self.assertEqual(row["source"], "emodul")
        self.assertEqual(row["source_key"], "controller-1:2026-11-26T10:00:00+00:00")
        self.assertEqual(row["observed_at"], int(datetime(2026, 11, 26, 10, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(row["recorded_at"], 1795687500)
        self.assertEqual(json.loads(row["payload_json"])["tiles"], self.payload["tiles"])

    def test_identical_replay_is_idempotent(self):
        kwargs = {"database_path": self.database, "module_id": "controller-1",
                  "recorded_at": 1795687500}
        self.assertEqual(persist_emodul_snapshot(self.payload, **kwargs)["status"], "inserted")
        self.assertEqual(persist_emodul_snapshot(self.payload, **kwargs)["status"], "duplicate")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)

    def test_same_source_timestamp_with_changed_payload_is_not_overwritten(self):
        kwargs = {"database_path": self.database, "module_id": "controller-1",
                  "recorded_at": 1795687500}
        persist_emodul_snapshot(self.payload, **kwargs)
        changed = {**self.payload, "data": {"temperatures": {"co": 66.0}}}
        self.assertEqual(persist_emodul_snapshot(changed, **kwargs)["status"], "conflict")
        with self.store.connection() as db:
            row = db.execute("SELECT payload_json FROM telemetry_samples").fetchone()
        self.assertEqual(json.loads(row["payload_json"])["data"]["temperatures"]["co"], 65.0)

    def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        result = persist_emodul_snapshot(self.payload, database_path=missing)
        self.assertEqual(result["reason"], "database_missing")
        self.assertFalse(missing.exists())

    def test_unmigrated_database_is_not_migrated_implicitly(self):
        empty = Path(self.temp.name) / "empty.sqlite3"
        empty.touch()
        result = persist_emodul_snapshot(self.payload, database_path=empty)
        self.assertEqual(result["reason"], "schema_not_ready")
        self.assertEqual(SQLiteStore(empty).schema_version(), 0)

    def test_disconnected_or_invalid_snapshots_are_skipped(self):
        disconnected = {**self.payload, "connected": False}
        self.assertEqual(persist_emodul_snapshot(disconnected, database_path=self.database)["reason"], "source_not_connected")
        invalid = {**self.payload, "fetched_at": "not-a-timestamp"}
        self.assertEqual(persist_emodul_snapshot(invalid, database_path=self.database)["reason"], "invalid_observation_timestamp")
        self.assertEqual(persist_emodul_snapshot({**self.payload, "data": None}, database_path=self.database)["reason"], "snapshot_data_missing")


if __name__ == "__main__":
    unittest.main()
