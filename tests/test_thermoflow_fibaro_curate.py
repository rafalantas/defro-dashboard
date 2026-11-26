"""Regression tests for safe Fibaro raw-history curation."""
import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_fibaro_curate import curate_database, FEATURE_SET


class FibaroCurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.store.migrate()
        snapshot = {
            "schema": "fibaro-hc3-snapshot/1",
            "zones": [
                {"id": 7, "name": "Salon", "properties": {"devices": [2162, 2170]}},
                {"id": 13, "name": "Hol", "properties": {"devices": [2297]}},
            ],
            "heads": [
                {"id": 2162, "name": "Grzejnik Salon Lewy", "type": "com.fibaro.FGT001", "properties": {}},
                {"id": 2170, "name": "Grzejnik Salon Prawy", "type": "com.fibaro.FGT001", "properties": {}},
                {"id": 2297, "name": "Grzejnik Hol Lewy", "type": "com.fibaro.FGT001", "properties": {}},
            ],
        }
        events = [
            {"id": 1, "timestamp": 900, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatingThermostatSetpoint", "oldValue": 21.5, "newValue": 21.0}},
            {"id": 2, "timestamp": 901, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "power", "oldValue": 10, "newValue": 11}},
            {"id": 3, "timestamp": 902, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5, "unit": "C"}},
            {"id": 4, "timestamp": 903, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2297, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 5, "timestamp": 904, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 9999, "property": "heatingThermostatSetpoint", "oldValue": 20.0, "newValue": 21.0}},
        ]
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fibaro_snapshots(captured_at,payload_json,payload_sha256) VALUES(?,?,?)",
                (1000, json.dumps(snapshot), "snapshot-hash"),
            )
            for event in events:
                db.execute(
                    """INSERT INTO fibaro_events(
                        event_id,observed_at,event_type,payload_json,payload_sha256,collected_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (event["id"], event["timestamp"], event["type"],
                     json.dumps(event), f"hash-{event['id']}", 1001),
                )
            db.execute(
                """INSERT INTO telemetry_samples(
                    source,source_key,observed_at,recorded_at,payload_json,payload_sha256
                ) VALUES('emodul','sample-1',1000,1001,'{}','telemetry-hash')"""
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_is_read_only_and_excludes_noise(self):
        result = curate_database(str(self.db_path), apply=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["raw_events"], 5)
        self.assertEqual(result["curated_features"], 3)
        self.assertEqual(result["skipped_by_reason"]["unrelated_head_property"], 1)
        self.assertEqual(result["skipped_by_reason"]["not_a_mapped_head"], 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[0], 0)

    def test_apply_backs_up_then_atomically_replaces_raw_events(self):
        result = curate_database(str(self.db_path), apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["raw_events_deleted"], 5)
        self.assertEqual(result["fibaro_events_remaining"], 0)
        self.assertEqual(result["sqlite_integrity"], "ok")
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(result["backup_integrity"], ["ok"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)
            rows = db.execute(
                "SELECT values_json FROM feature_windows WHERE feature_set=? ORDER BY window_start",
                (FEATURE_SET,),
            ).fetchall()
            self.assertEqual(len(rows), 3)
            features = [json.loads(row["values_json"]) for row in rows]
            transitions = [
                transition for feature in features
                for transition in feature["transitions"]
            ]
            self.assertEqual(
                {item["signal"] for item in transitions},
                {"setpoint", "zone_temperature", "heat_required"},
            )
            self.assertTrue(all(
                item["mapping_basis"] == "nearest_available_snapshot_after_event"
                for item in transitions
            ))
            self.assertEqual(
                {feature["schema"] for feature in features},
                {"thermoflow-fibaro-heating-feature/1"},
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM datasets WHERE name='fibaro_heating_history'").fetchone()[0], 1)

    def test_missing_snapshot_fails_without_deleting_raw_events(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM fibaro_snapshots")
        with self.assertRaises(RuntimeError):
            curate_database(str(self.db_path), apply=True)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()
