"""Tests for the read-only curated analytical store."""
import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_analytics_store import (
    DATASET_NAME, FEATURE_SCHEMA, FEATURE_SET,
    read_analytics_inventory, read_curated_fibaro_features,
)


class AnalyticsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.migrate()
        values = {"schema": FEATURE_SCHEMA, "source": "fibaro-hc3",
                  "observed_at": 1100, "transitions": [
                      {"signal": "heat_required", "entity": "head",
                       "device_id": 12, "zone_id": None,
                       "historical_zone_mapping": "unverified",
                       "old_value": False, "new_value": True}]}
        with self.store.connection() as db:
            db.execute("INSERT INTO fibaro_events VALUES(1,1100,'noise','{}','raw',1200)")
            db.execute(
                "INSERT INTO feature_windows(feature_set,window_start,window_end,generated_at,"
                "dataset_version,values_json) VALUES(?,?,?,?,?,?)",
                (FEATURE_SET, 1100, 1101, 1200, "v2-test", json.dumps(values)),
            )
            manifest = {"schema": "thermoflow-fibaro-curated-dataset/2",
                        "source_event_payloads_retained": True}
            db.execute(
                "INSERT INTO datasets(name,version,created_at,manifest_json,content_sha256,status) "
                "VALUES(?,?,?,?,?,?)",
                (DATASET_NAME, "v2-test", 1200, json.dumps(manifest), "digest", "candidate"),
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_inventory_separates_raw_archive_from_analytics(self):
        result = read_analytics_inventory(self.path)
        self.assertEqual(result["storage"]["raw_archive"]["fibaro_events"], 1)
        self.assertEqual(result["storage"]["analytical"]["fibaro_v2_feature_windows"], 1)
        self.assertFalse(result["policy"]["database_modified"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 1)

    def test_reader_uses_only_candidate_v2_dataset(self):
        result = read_curated_fibaro_features(self.path, start=1000, end=2000)
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["raw_events_used"])
        self.assertEqual(result["features"][0]["values"]["schema"], FEATURE_SCHEMA)

    def test_reader_does_not_fall_back_to_raw_events(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM datasets")
        result = read_curated_fibaro_features(self.path, start=1000, end=2000)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["count"], 0)

    def test_invalid_window_is_rejected(self):
        with self.assertRaises(ValueError):
            read_curated_fibaro_features(self.path, start=10, end=10)


if __name__ == "__main__":
    unittest.main()
