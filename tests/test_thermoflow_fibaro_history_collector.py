"""Regression tests for the durable Fibaro history collector."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_history_collector import FibaroHistoryStore


class FakeProvider:
    def __init__(self, events):
        self.events = events
        self.requested = None

    def get_events_range(self, start, end):
        self.requested = (start, end)
        return self.events


class FibaroHistoryCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = FibaroHistoryStore(self.path, retention_days=30)
        self.events = [
            {"id": 11, "timestamp": 1000, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 12, "timestamp": 1010, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5}},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_collect_once_uses_only_the_read_only_provider_and_persists_events(self):
        provider = FakeProvider(self.events)
        result = self.store.collect_once(provider, start=900, end=1100, collected_at=1200)
        self.assertEqual(provider.requested, (900, 1100))
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.read_events(), self.events)
        self.assertTrue(self.store.status()["persistent"])
        self.assertTrue(self.store.status()["read_only_source"])

    def test_repeated_collection_is_idempotent_by_hc3_event_id(self):
        provider = FakeProvider(self.events)
        first = self.store.collect_once(provider, start=900, end=1100, collected_at=1200)
        second = self.store.collect_once(provider, start=900, end=1100, collected_at=1300)
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(len(self.store.read_events()), 2)

    def test_invalid_events_are_counted_and_mark_run_partial(self):
        events = self.events + [
            {"id": 0, "timestamp": 1020, "type": "bad"},
            {"id": 13, "timestamp": 999999, "type": "outside-window"},
        ]
        result = self.store.save_batch(events, start=900, end=1100, collected_at=1200)
        self.assertEqual(result["invalid"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.status()["last_run"]["status"], "partial")

    def test_cursor_never_moves_backwards(self):
        self.store.save_batch(self.events, start=900, end=1100, collected_at=1200)
        self.store.save_batch([], start=800, end=900, collected_at=1300)
        self.assertEqual(self.store.status()["last_successful_end"], 1100)

    def test_retention_prunes_old_events_without_touching_new_events(self):
        self.store.save_batch(self.events, start=900, end=1100, collected_at=1200)
        result = self.store.prune(now=1200 + 31 * 86400)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(self.store.read_events(), [])

    def test_rejects_unbounded_windows_and_limits(self):
        with self.assertRaises(ValueError):
            self.store.save_batch([], start=1, end=1 + 8 * 86400, collected_at=1200)
        with self.assertRaises(ValueError):
            self.store.read_events(limit=0)


if __name__ == "__main__":
    unittest.main()
