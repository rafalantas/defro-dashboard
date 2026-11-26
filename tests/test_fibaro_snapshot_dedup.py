"""Tests for content-based deduplication of persisted Fibaro snapshots."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_store import SQLiteStore


class FibaroSnapshotDedupTests(unittest.TestCase):
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

    def test_same_payload_at_later_time_updates_observation_without_new_row(self):
        snapshot = {"zones": [{"id": 7}], "heads": [{"id": 2162}]}
        first = self.collector.save_snapshot(snapshot, captured_at=1000)
        second = self.collector.save_snapshot(snapshot, captured_at=1100)
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["captured_at"], 1000)
        self.assertEqual(second["last_observed_at"], 1100)
        latest = self.collector.read_latest_snapshot()
        self.assertEqual(latest["captured_at"], 1000)
        self.assertEqual(latest["last_observed_at"], 1100)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_snapshots").fetchone()[0], 1)
            state = db.execute(
                "SELECT cursor_value,updated_at FROM collector_state "
                "WHERE collector_name='fibaro_hc3_snapshot'"
            ).fetchone()
            self.assertEqual(state["cursor_value"], "1100")
            self.assertEqual(state["updated_at"], 1100)

    def test_changed_payload_creates_new_snapshot(self):
        first = self.collector.save_snapshot({"zones": []}, captured_at=1000)
        second = self.collector.save_snapshot({"zones": [{"id": 7}]}, captured_at=1100)
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(second["status"], "inserted")
        self.assertNotEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(self.collector.read_latest_snapshot()["payload"], {"zones": [{"id": 7}]})

    def test_observation_cursor_never_moves_backwards(self):
        snapshot = {"zones": []}
        self.collector.save_snapshot(snapshot, captured_at=1100)
        self.collector.save_snapshot(snapshot, captured_at=1000)
        latest = self.collector.read_latest_snapshot()
        self.assertEqual(latest["last_observed_at"], 1100)

    def test_returning_to_previous_payload_selects_latest_observation(self):
        snapshot_a = {"zones": [{"id": 1}], "heads": [{"id": 11}]}
        snapshot_b = {"zones": [{"id": 2}], "heads": [{"id": 22}]}
        self.collector.save_snapshot(snapshot_a, captured_at=1000)
        self.collector.save_snapshot(snapshot_b, captured_at=1100)
        returned = self.collector.save_snapshot(snapshot_a, captured_at=1200)
        self.assertEqual(returned["status"], "duplicate")
        latest = self.collector.read_latest_snapshot()
        self.assertEqual(latest["payload"], snapshot_a)
        self.assertEqual(latest["last_observed_at"], 1200)

    def test_empty_store_has_no_snapshot(self):
        self.assertIsNone(self.collector.read_latest_snapshot())


if __name__ == "__main__":
    unittest.main()
