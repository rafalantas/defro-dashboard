"""End-to-end regression: fake HC3 -> SQLite -> read-only readiness audit."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_collection import collect_fibaro_once
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_fibaro_readiness import inspect_readiness
from thermoflow_store import SQLiteStore


class FakeHC3:
    def get_climate_zones(self):
        return [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}]

    def get_climate_zone(self, zone_id):
        return {"id": zone_id, "name": "Salon", "properties": {"devices": [2162]}}

    def get_device(self, device_id):
        return {"id": device_id, "name": "Głowica", "type": "com.fibaro.FGT001",
                "properties": {"heatRequired": True, "batteryLevel": 90}}

    def get_events_range(self, start, end):
        return [{"id": 44, "timestamp": 1000, "type": "DevicePropertyUpdatedEvent",
                 "data": {"id": 2162, "property": "heatRequired",
                          "oldValue": False, "newValue": True}}]


class FibaroSQLiteEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        store.migrate()
        self.collector = FibaroHistoryCollector(store)
        self.provider = FakeHC3()

    def tearDown(self):
        self.temp.cleanup()

    def test_collection_is_idempotent_and_readiness_reads_persisted_evidence(self):
        first = collect_fibaro_once(
            self.provider, self.collector, start=900, end=1200, collected_at=1100
        )
        second = collect_fibaro_once(
            self.provider, self.collector, start=900, end=1200, collected_at=1200
        )
        self.assertEqual(first["events"]["inserted"], 1)
        self.assertEqual(second["events"]["inserted"], 0)
        self.assertEqual(second["events"]["duplicates"], 1)
        self.assertEqual(first["snapshot"]["status"], "inserted")
        self.assertEqual(second["snapshot"]["status"], "duplicate")

        audit = inspect_readiness(self.path, start=900, end=1200, now=1300)
        self.assertEqual(audit["status"], "ready")
        self.assertEqual(audit["events"]["count"], 1)
        self.assertEqual(audit["snapshots"]["transitions_in_window"], 1)
        self.assertEqual(audit["collection_runs"]["count"], 2)
        self.assertEqual(audit["collection_runs"]["successful"], 2)
        self.assertTrue(audit["semantics"]["history_is_event_driven"])
        self.assertFalse(audit["semantics"]["empty_event_interval_proves_outage"])
        self.assertTrue(audit["read_only"])
        self.assertFalse(audit["automatic_quality_promotion"])


if __name__ == "__main__":
    unittest.main()
