"""Regression tests for the explicit Fibaro one-shot collection runner."""
import tempfile
import unittest
from pathlib import Path
from thermoflow_fibaro_collection import collect_fibaro_once
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_store import SQLiteStore


class FakeProvider:
    def get_climate_zones(self):
        return [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}]
    def get_climate_zone(self, zone_id):
        return {"id": zone_id, "name": "Salon", "properties": {"devices": [2162]}}
    def get_device(self, device_id):
        return {"id": device_id, "name": "Głowica", "type": "com.fibaro.FGT001",
                "properties": {"heatRequired": True}}
    def get_events_range(self, start, end):
        self.requested = (start, end)
        return [{"id": 11, "timestamp": 1000, "type": "DevicePropertyUpdatedEvent",
                 "data": {"id": 2162, "property": "heatRequired",
                          "oldValue": False, "newValue": True}}]


class FibaroCollectionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStore(
            root / "thermoflow.sqlite3",
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store)
        self.provider = FakeProvider()
    def tearDown(self):
        self.temp.cleanup()
    def test_one_shot_collects_events_and_snapshot_and_reports_coverage(self):
        result = collect_fibaro_once(
            self.provider, self.collector, start=900, end=1100, collected_at=1200
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["events"]["inserted"], 1)
        self.assertEqual(result["snapshot"]["zones"], 1)
        self.assertEqual(result["coverage"]["stored_events_in_window"], 1)
        self.assertEqual(self.provider.requested, (900, 1100))
        self.assertTrue(result["safety"]["read_only_upstream"])
        self.assertFalse(result["safety"]["background_worker_started"])
        self.assertEqual(self.collector.read_latest_snapshot()["captured_at"], 1200)
    def test_repeated_run_is_idempotent(self):
        first = collect_fibaro_once(
            self.provider, self.collector, start=900, end=1100, collected_at=1200
        )
        second = collect_fibaro_once(
            self.provider, self.collector, start=900, end=1100, collected_at=1300
        )
        self.assertEqual(first["events"]["inserted"], 1)
        self.assertEqual(second["events"]["inserted"], 0)
        self.assertEqual(second["events"]["duplicates"], 1)
    def test_rejects_unbounded_or_invalid_windows(self):
        for start, end in ((0, 10), (10, 10), (20, 10), (1, 1 + 8 * 86400)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                collect_fibaro_once(self.provider, self.collector, start=start, end=end)


if __name__ == "__main__":
    unittest.main()
