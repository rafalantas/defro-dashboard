"""Regression tests for the safe Fibaro ML candidate dataset."""
import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_fibaro_curate import (
    DATASET_NAME,
    FEATURE_SET,
    build_curated_rows,
    curate_database,
)


class FibaroCandidateDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.backup_dir = Path(self.temp.name) / "backups"
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
            {"id": 6, "timestamp": 905, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2170, "property": "windowOpened", "oldValue": False, "newValue": True}},
            {"id": 7, "timestamp": 906, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatingThermostatSetpoint", "oldValue": 21.0, "newValue": 21.0}},
            {"id": 8, "timestamp": 900, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatingThermostatSetpoint", "oldValue": 21.5, "newValue": 21.0}},
            {"id": 9, "timestamp": 907, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": "false", "newValue": True}},
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
                    (
                        event["id"],
                        event["timestamp"],
                        event["type"],
                        json.dumps(event),
                        f"hash-{event['id']}",
                        1001,
                    ),
                )
            db.execute(
                """INSERT INTO telemetry_samples(
                    source,source_key,observed_at,recorded_at,payload_json,payload_sha256
                ) VALUES('emodul','sample-1',1000,1001,'{}','telemetry-hash')"""
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_is_read_only_and_filters_to_useful_signals(self):
        result = curate_database(str(self.db_path), apply=False)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["raw_events"], 9)
        self.assertEqual(result["curated_features"], 4)
        self.assertEqual(result["feature_windows"], 4)
        self.assertEqual(
            result["signal_counts"],
            {
                "heat_required": 1,
                "setpoint": 1,
                "window_open": 1,
                "zone_temperature": 1,
            },
        )
        self.assertEqual(result["skipped_by_reason"]["unrelated_device_property"], 1)
        self.assertEqual(result["skipped_by_reason"]["unknown_head_id"], 1)
        self.assertEqual(result["skipped_by_reason"]["unchanged_value"], 1)
        self.assertEqual(result["skipped_by_reason"]["duplicate_transition"], 1)
        self.assertEqual(result["skipped_by_reason"]["invalid_boolean_transition"], 1)
        self.assertEqual(result["historical_head_zone_mapping"], "event_time_snapshot_only")
        self.assertTrue(result["raw_source_events_preserved"])

        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0], 0)

    def test_head_features_do_not_claim_historical_zone_membership(self):
        with self.store.connection() as db:
            features, report = build_curated_rows(db)

        transitions = [
            transition
            for feature in features
            for transition in json.loads(feature["values_json"])["transitions"]
        ]
        head_transitions = [item for item in transitions if item["entity"] == "head"]
        zone_transitions = [item for item in transitions if item["entity"] == "zone"]

        # The only snapshot in this fixture is at t=1000. Events at t=900..907
        # must not borrow its future mapping retroactively.
        self.assertEqual(len(head_transitions), 0)
        self.assertEqual(len(zone_transitions), 1)
        self.assertEqual(zone_transitions[0]["zone_id"], 7)
        self.assertEqual(zone_transitions[0]["identity_basis"], "zone_id_from_event_payload")
        self.assertEqual(report["snapshots_used"], 1)
        self.assertEqual(report["head_zone_mapping"]["verified"], 0)
        self.assertEqual(report["head_zone_mapping"]["unmapped"], 3)
        self.assertFalse(report["head_zone_mapping"]["future_snapshot_fallback"])
        self.assertEqual(report["skipped_by_reason"]["event_without_prior_snapshot"], 3)

    def test_dataset_hash_is_deterministic(self):
        with self.store.connection() as db:
            _, first = build_curated_rows(db)
        with self.store.connection() as db:
            _, second = build_curated_rows(db)
        self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_apply_persists_candidate_and_retains_all_raw_source_events(self):
        result = curate_database(
            str(self.db_path), apply=True, backup_dir=str(self.backup_dir)
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["raw_events_deleted"], 0)
        self.assertEqual(result["fibaro_events_remaining"], 9)
        self.assertTrue(result["raw_source_events_preserved"])
        self.assertEqual(result["dataset_status"], "candidate")
        self.assertEqual(result["sqlite_integrity"], "ok")
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(result["backup_integrity"], ["ok"])

        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_snapshots").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)
            rows = db.execute(
                "SELECT values_json FROM feature_windows WHERE feature_set=? ORDER BY window_start",
                (FEATURE_SET,),
            ).fetchall()
            self.assertEqual(len(rows), 4)
            transitions = [
                item
                for row in rows
                for item in json.loads(row["values_json"])["transitions"]
            ]
            self.assertEqual(
                {item["signal"] for item in transitions},
                {"zone_temperature"},
            )
            self.assertEqual(
                {json.loads(row["values_json"])["schema"] for row in rows},
                {"thermoflow-fibaro-heating-feature/3"},
            )
            dataset = db.execute(
                "SELECT status,manifest_json FROM datasets WHERE name=?",
                (DATASET_NAME,),
            ).fetchone()
            self.assertIsNotNone(dataset)
            self.assertEqual(dataset["status"], "candidate")
            manifest = json.loads(dataset["manifest_json"])
            self.assertTrue(manifest["source_event_payloads_retained"])
            self.assertEqual(manifest["historical_head_zone_mapping"], "event_time_snapshot_only")

    def test_missing_snapshot_fails_without_writing_or_deleting(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM fibaro_snapshots")

        with self.assertRaises(RuntimeError):
            curate_database(
                str(self.db_path), apply=True, backup_dir=str(self.backup_dir)
            )

        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
