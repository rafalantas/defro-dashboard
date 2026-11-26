import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_analytics_store import DATASET_NAME, FEATURE_SET, FEATURE_SCHEMA
from thermoflow_heating_diagnostics import read_heating_analytics, persist_heating_aggregates


class HeatingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.migrate()
        stamp = 1795712400
        feature = {"schema": FEATURE_SCHEMA, "source": "fibaro-hc3",
                   "observed_at": stamp, "transitions": [
                       {"source_event_id": 1, "signal": "heat_required", "entity": "head",
                        "device_id": 10, "zone_id": None,
                        "historical_zone_mapping": "unverified",
                        "old_value": False, "new_value": True}]}
        with self.store.connection() as db:
            db.execute("INSERT INTO fibaro_events VALUES(1,?,?, '{}','raw',?)",
                       (stamp, "DevicePropertyUpdatedEvent", stamp + 10))
            db.execute("INSERT INTO feature_windows(feature_set,window_start,window_end,"
                       "generated_at,dataset_version,values_json) VALUES(?,?,?,?,?,?)",
                       (FEATURE_SET, stamp, stamp + 1, stamp + 20, "v2-test", json.dumps(feature)))
            db.execute("INSERT INTO datasets(name,version,created_at,manifest_json,content_sha256,status)"
                       " VALUES(?,?,?,?,?,?)",
                       (DATASET_NAME, "v2-test", stamp + 20, "{}", "digest", "candidate"))
            sample = {"schema": "thermoflow-emodul-ml-input/1", "source": "emodul",
                      "observed_at": stamp + 30,
                      "data": {"temperatures": {"co": 65.0, "return": 55.0,
                                                "flue": 90.0, "outdoor": 4.0},
                               "boiler_power_kw": 25.0}}
            db.execute("INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,"
                       "payload_json,payload_sha256) VALUES(?,?,?,?,?,?)",
                       ("emodul", "sample-1", stamp + 30, stamp + 40,
                        json.dumps(sample), "telemetry-hash"))

    def tearDown(self):
        self.temp.cleanup()

    def test_end_to_end_reads_curated_only_and_builds_diagnostics(self):
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["source_counts"]["fibaro_transitions"], 1)
        self.assertEqual(result["diagnostics"]["event_counts_by_signal"]["heat_required"], 1)
        self.assertEqual(result["diagnostics"]["head_events_with_unverified_zone_mapping"], 1)
        self.assertEqual(result["diagnostics"]["telemetry_context_after_heat_required_transition"]["true"]
                         ["temperatures.co"]["mean"], 65.0)
        self.assertFalse(result["persistence"]["writes"])

    def test_aggregate_persistence_is_idempotent_and_candidate_only(self):
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500)
        persisted = persist_heating_aggregates(self.path, result["aggregates"], recorded_at=1800000000)
        again = persist_heating_aggregates(self.path, result["aggregates"], recorded_at=1800000001)
        self.assertEqual(persisted["status"], "persisted")
        self.assertEqual(persisted["dataset_version"], again["dataset_version"])
        self.assertEqual(again["inserted_feature_windows"], 0)
        with self.store.connection() as db:
            row = db.execute("SELECT status FROM datasets WHERE name='thermoflow_heating_aggregates'").fetchone()
            self.assertEqual(row["status"], "candidate")

    def test_no_candidate_dataset_fails_closed(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM datasets")
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500)
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["aligned"])


if __name__ == "__main__":
    unittest.main()
