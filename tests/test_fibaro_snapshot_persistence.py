"""Tests for immutable Fibaro snapshot persistence in the shared SQLite store."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_store import SQLiteStore


class FibaroSnapshotPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_is_persisted_and_read_without_device_access(self):
        snapshot = {
            "zones": [{"id": 7, "name": "Salon"}],
            "heads": [{"id": 2162, "properties": {"heatRequired": True}}],
        }
        saved = self.collector.save_snapshot(snapshot, captured_at=1000)
        loaded = self.collector.read_latest_snapshot()
        self.assertEqual(saved["status"], "inserted")
        self.assertEqual(loaded["captured_at"], 1000)
        self.assertEqual(loaded["payload"], snapshot)
        self.assertEqual(loaded["payload_sha256"], saved["payload_sha256"])

    def test_identical_snapshot_is_idempotent(self):
        snapshot = {"zones": [], "heads": []}
        first = self.collector.save_snapshot(snapshot, captured_at=1000)
        second = self.collector.save_snapshot(snapshot, captured_at=1000)
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["id"], second["id"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_snapshots").fetchone()[0], 1)

    def test_changed_snapshot_at_same_time_is_preserved_as_distinct_evidence(self):
        first = self.collector.save_snapshot({"zones": []}, captured_at=1000)
        second = self.collector.save_snapshot({"zones": [{"id": 7}]}, captured_at=1000)
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(second["status"], "inserted")
        self.assertNotEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(self.collector.read_latest_snapshot()["payload"], {"zones": [{"id": 7}]})

    def test_invalid_or_non_json_snapshot_is_rejected(self):
        with self.assertRaises(ValueError):
            self.collector.save_snapshot([], captured_at=1000)
        with self.assertRaises(ValueError):
            self.collector.save_snapshot({"temperature": float("nan")}, captured_at=1000)

    def test_missing_snapshot_returns_none(self):
        self.assertIsNone(self.collector.read_latest_snapshot())


if __name__ == "__main__":
    unittest.main()
