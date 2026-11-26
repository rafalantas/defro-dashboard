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


    def test_thingspeak_samples_are_aligned_and_not_reused(self):
        stamp = 1795712400
        with self.store.connection() as db:
            for offset, entry in ((20, 1), (25, 2)):
                payload = {"field1": "68.5", "field2": "55.0"}
                db.execute(
                    "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,"
                    "payload_json,payload_sha256) VALUES(?,?,?,?,?,?)",
                    ("thingspeak:temperatures_power", f"channel:{entry}", stamp + offset,
                     stamp + 100, json.dumps(payload), f"ts-hash-{entry}"),
                )
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500,
                                        tolerance_seconds=90)
        self.assertEqual(result["source_counts"]["thingspeak_samples"], 2)
        self.assertEqual(result["aligned"]["counts"]["matched_by_source"]["thingspeak:temperatures_power"], 1)
        self.assertIn("thingspeak:temperatures_power", result["aligned"]["observations"][0]["telemetry"])
        self.assertEqual(result["aligned"]["counts"]["matched"], 1)
        metric = result["aggregates"]["hourly"][0]["metrics"]["thingspeak:temperatures_power.field1"]
        self.assertEqual(metric["mean"], 68.5)
        self.assertEqual(result["aggregates"]["hourly"][0]["unique_telemetry_samples"], 2)
        self.assertEqual(result["aggregates"]["hourly"][0]["unique_emodul_samples"], 1)


    def test_refill_event_feed_is_not_treated_as_heating_telemetry(self):
        stamp = 1795712400
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,"
                "payload_json,payload_sha256) VALUES(?,?,?,?,?,?)",
                ("thingspeak:fuel_refills_raw", "fuel:1", stamp + 20, stamp + 100,
                 json.dumps({"field1": "15"}), "fuel-raw-hash"),
            )
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500)
        self.assertEqual(result["source_counts"]["thingspeak_samples"], 0)
        self.assertNotIn("thingspeak:fuel_refills_raw",
                         result["aligned"]["counts"]["matched_by_source"])

    def test_thingSpeak_fields_are_kept_as_raw_numeric_features(self):
        from thermoflow_time_alignment import align_observations
        feature = {"values": {"schema": "thermoflow-fibaro-heating-feature/2",
                              "observed_at": 1000, "transitions": [
                                  {"source_event_id": 1, "signal": "setpoint"}]}}
        samples = [{"source": "thingspeak:valve_pumps_fuel", "source_key": "c:1",
                    "observed_at": 1005, "payload_json": json.dumps({"field1": "42.5"})}]
        aligned = align_observations([feature], samples, tolerance_seconds=10)
        sample = aligned["observations"][0]["telemetry"]["thingspeak:valve_pumps_fuel"]
        self.assertEqual(sample["data"]["field1"], "42.5")

    def test_no_candidate_dataset_fails_closed(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM datasets")
        result = read_heating_analytics(self.path, start=1795712300, end=1795712500)
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["aligned"])


if __name__ == "__main__":
    unittest.main()
