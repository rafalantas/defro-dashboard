"""Tests for read-only Fibaro SQLite API adapters."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_fibaro_store_api import (
    read_fibaro_store_history, read_fibaro_store_shadow, read_fibaro_store_status,
)


class FibaroStoreApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store)
        self.collector.save_snapshot({"zones": [], "heads": []}, captured_at=1000)
        self.collector.save_batch(
            [{"id": 1, "timestamp": 1100, "type": "ClimateZoneTemperatureChangedEvent",
              "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5}}],
            start=1000, end=1200, collected_at=1300,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_status_reports_persistent_store_health(self):
        result = read_fibaro_store_status(self.path)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["collector"]["events"], 1)
        self.collector.mark_backfill_complete(1200, recorded_at=1300, initial_start=1000)
        result = read_fibaro_store_status(self.path)
        self.assertTrue(result["backfill"]["backfill_complete"])
        self.assertEqual(result["backfill"]["last_successful_end"], 1200)
        self.assertTrue(result["integrity_check"]["ok"])
        self.assertTrue(result["read_only"])

    def test_history_reads_events_and_snapshot_history(self):
        result = read_fibaro_store_history(self.path, start=1000, end=1200)
        self.assertEqual(result["counts"], {"events": 1, "snapshots": 1})
        self.assertEqual(result["events"][0]["id"], 1)
        self.assertTrue(result["read_only"])

    def test_shadow_pipeline_uses_sqlite_and_does_not_persist(self):
        before = self.store.schema_version()
        result = read_fibaro_store_shadow(self.path, start=1000, end=1200)
        self.assertEqual(result["source"], "shared_sqlite")
        self.assertFalse(result["safety"]["local_store_writes"])
        self.assertEqual(self.store.schema_version(), before)

    def test_invalid_window_and_missing_database_fail_closed(self):
        with self.assertRaises(ValueError):
            read_fibaro_store_history(self.path, start=100, end=100 + 91 * 86400)
        with self.assertRaises(FileNotFoundError):
            read_fibaro_store_status(self.path.parent / "missing.sqlite3")


if __name__ == "__main__":
    unittest.main()
