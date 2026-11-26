"""Regression tests for the explicit Fibaro one-shot collection runner."""
import tempfile
import unittest
from pathlib import Path
from thermoflow_fibaro_collection import collect_fibaro_once, collect_fibaro_incremental, collect_fibaro_backfill
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
        event = {"id": 11, "timestamp": 1000, "type": "DevicePropertyUpdatedEvent",
                 "data": {"id": 2162, "property": "heatRequired",
                          "oldValue": False, "newValue": True}}
        return [event] if start <= 1000 <= end else []


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
    def test_incremental_requires_initial_start_and_advances_cursor(self):
        with self.assertRaisesRegex(ValueError, "initial_start"):
            collect_fibaro_incremental(self.provider, self.collector, now=1100)
        first = collect_fibaro_incremental(
            self.provider, self.collector, now=1100, initial_start=900,
            overlap_seconds=30, collected_at=1200,
        )
        self.assertEqual(first["window"]["start"], 900)
        self.assertEqual(first["cursor_after"], 1100)
        second = collect_fibaro_incremental(
            self.provider, self.collector, now=1200, initial_start=900,
            overlap_seconds=30, collected_at=1300,
        )
        self.assertEqual(second["window"]["start"], 1070)
        self.assertEqual(second["window"]["end"], 1200)
        self.assertEqual(second["cursor_after"], 1200)

    def test_incremental_catches_up_in_bounded_chunks(self):
        result = collect_fibaro_incremental(
            self.provider, self.collector, now=900 + 9 * 86400,
            initial_start=900, collected_at=1200,
        )
        self.assertEqual(result["window"]["duration_seconds"], 7 * 86400)
        self.assertTrue(result["catch_up_pending"])

    def test_backfill_uses_separate_cursor_and_fills_history_before_old_live_cursor(self):
        # Simulate a previous partial import whose live cursor starts much later
        # than the requested backfill range. Backfill must not skip the earlier gap.
        self.collector.save_batch([], start=5000, end=6000, collected_at=6100)
        target = 900 + 8 * 86400
        result = collect_fibaro_backfill(
            self.provider, self.collector, now=target,
            initial_start=900, collected_at=target + 10,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["chunks_completed"], 2)
        self.assertEqual(self.provider.requested, (900 + 7 * 86400, target))
        self.assertEqual(self.collector.status()["last_successful_end"], target)
        backfill = self.collector.status("fibaro_hc3_backfill")
        self.assertEqual(backfill["last_successful_end"], target)
        self.assertTrue(backfill["backfill_complete"])
        self.assertEqual(self.collector.event_coverage(start=900, end=target)["stored_events"], 1)

    def test_rejects_unbounded_or_invalid_windows(self):
        for start, end in ((0, 10), (10, 10), (20, 10), (1, 1 + 8 * 86400)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                collect_fibaro_once(self.provider, self.collector, start=start, end=end)


if __name__ == "__main__":
    unittest.main()
