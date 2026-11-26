"""Tests for curated eModul AI/ML telemetry persistence."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from thermoflow_emodul_store import (
    SAMPLE_SCHEMA, persist_emodul_snapshot, select_ml_fields,
)
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
            "controller": {"name": "Defro", "type": "APC", "metadata": {"secret": "omit"}},
            "data": {
                "temperatures": {"co": 65.0, "flue": 90.0, "outdoor": 2.0},
                "boiler_power_kw": 12.5,
                "pumps": {"floor": True, "dhw": False},
                "valve": {"return": 55.0, "opening": 35.0},
                "fuel": {"percent": 72},
                "fire": True,
                "setpoints": {"co": 70.0},
                "hysteresis": {"boiler": 6.0},
                "other_tiles": [{"label": "UI-only", "value": 999}],
                "status_tiles": [{"header": "UI-only"}],
                "zones": [{"name": "UI-only"}],
                "menus": [{"id": 123, "value": 999}],
                "statistics": {"month": {"kg": 999}},
            },
            "tiles": [{"id": 1, "value": 65}],
            "zones": [{"name": "UI-only"}],
            "menus": [{"id": 123}],
            "statistics": {},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_curated_ml_sample_and_provenance(self):
        result = persist_emodul_snapshot(
            self.payload, database_path=self.database, module_id="controller-1",
            recorded_at=1795687500,
        )
        self.assertEqual(result["status"], "inserted")
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM telemetry_samples").fetchone()
        sample = json.loads(row["payload_json"])
        self.assertEqual(row["source"], "emodul")
        self.assertEqual(row["source_key"], "controller-1:2026-11-26T10:00:00+00:00")
        self.assertEqual(row["observed_at"], int(datetime(2026, 11, 26, 10, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(row["recorded_at"], 1795687500)
        self.assertEqual(sample["schema"], SAMPLE_SCHEMA)
        self.assertEqual(sample["data"]["temperatures"]["co"], 65.0)
        self.assertEqual(sample["data"]["pumps"]["floor"], True)
        self.assertNotIn("zones", sample)
        self.assertNotIn("menus", sample)
        self.assertNotIn("statistics", sample)
        self.assertNotIn("other_tiles", sample["data"])
        self.assertNotIn("status_tiles", sample["data"])
        self.assertNotIn("controller", sample)

    def test_allowlist_omits_unknown_and_non_finite_values(self):
        selected = select_ml_fields({
            "temperatures": {"co": 65.0, "unknown_sensor": 12, "bad": float("nan")},
            "pumps": {"floor": True, "unexpected": "x"},
            "zones": [{"name": "not-ml-input"}],
        })
        self.assertEqual(selected, {
            "temperatures": {"co": 65.0},
            "pumps": {"floor": True},
        })

    def test_identical_replay_is_idempotent(self):
        kwargs = {"database_path": self.database, "module_id": "controller-1",
                  "recorded_at": 1795687500}
        self.assertEqual(persist_emodul_snapshot(self.payload, **kwargs)["status"], "inserted")
        self.assertEqual(persist_emodul_snapshot(self.payload, **kwargs)["status"], "duplicate")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)

    def test_same_source_timestamp_with_changed_ml_data_is_not_overwritten(self):
        kwargs = {"database_path": self.database, "module_id": "controller-1",
                  "recorded_at": 1795687500}
        persist_emodul_snapshot(self.payload, **kwargs)
        changed = {**self.payload, "data": {**self.payload["data"],
                                            "temperatures": {"co": 66.0}}}
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

    def test_disconnected_invalid_or_non_ml_samples_are_skipped(self):
        disconnected = {**self.payload, "connected": False}
        self.assertEqual(persist_emodul_snapshot(disconnected, database_path=self.database)["reason"], "source_not_connected")
        invalid = {**self.payload, "fetched_at": "not-a-timestamp"}
        self.assertEqual(persist_emodul_snapshot(invalid, database_path=self.database)["reason"], "invalid_observation_timestamp")
        empty = {**self.payload, "data": {"zones": [], "menus": []}}
        self.assertEqual(persist_emodul_snapshot(empty, database_path=self.database)["reason"], "no_ml_fields")


if __name__ == "__main__":
    unittest.main()
