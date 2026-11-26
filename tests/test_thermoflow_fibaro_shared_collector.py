"""Regression tests for Fibaro ingestion into the shared ThermoFlow SQLite DB."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_fibaro_history_collector import FibaroHistoryCollector


class FakeProvider:
    def __init__(self, events):
        self.events = events
        self.requested = None

    def get_events_range(self, start, end):
        self.requested = (start, end)
        return self.events


class SharedFibaroCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SQLiteStore(
            self.root / "thermoflow.sqlite3",
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store, retention_days=30)
        self.events = [
            {"id": 11, "timestamp": 1000, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 12, "timestamp": 1010, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5}},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def test_collector_uses_read_only_provider_and_shared_schema(self):
        provider = FakeProvider(self.events)
        result = self.collector.collect_once(provider, start=900, end=1100, collected_at=1200)
        self.assertEqual(provider.requested, (900, 1100))
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.collector.read_events(), self.events)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 1)

    def test_repeated_collection_is_idempotent(self):
        first = self.collector.save_batch(self.events, start=900, end=1100, collected_at=1200)
        second = self.collector.save_batch(self.events, start=900, end=1100, collected_at=1300)
        self.assertEqual(first["inserted"], 2)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(len(self.collector.read_events()), 2)

    def test_event_id_payload_conflict_is_rejected_not_overwritten(self):
        self.collector.save_batch(self.events, start=900, end=1100, collected_at=1200)
        changed = [dict(self.events[0], timestamp=1001)]
        result = self.collector.save_batch(changed, start=900, end=1100, collected_at=1300)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.collector.read_events()[0]["timestamp"], 1000)

    def test_recorded_failure_does_not_advance_cursor_and_success_recovers(self):
        failure = self.collector.record_failure(
            error_type="TimeoutError", attempted_at=1150,
            window_start=900, window_end=1100,
        )
        self.assertFalse(failure["cursor_advanced"])
        self.assertIsNone(self.collector.status()["last_successful_end"])
        result = self.collector.save_batch([], start=900, end=1100, collected_at=1200)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.collector.status()["last_successful_end"], 1100)

    def test_event_coverage_uses_exact_sql_counts(self):
        self.collector.save_batch(self.events, start=900, end=1100, collected_at=1200)
        coverage = self.collector.event_coverage(start=900, end=1100)
        self.assertEqual(coverage, {
            "stored_events": 2,
            "first_event_timestamp": 1000,
            "last_event_timestamp": 1010,
        })

    def test_partial_batch_does_not_advance_shared_cursor(self):
        bad = self.events + [{"id": 0, "timestamp": 1020, "type": "bad"}]
        result = self.collector.save_batch(bad, start=900, end=1100, collected_at=1200)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(self.collector.status()["last_successful_end"])

    def test_shared_cursor_is_monotonic(self):
        self.collector.save_batch([], start=900, end=1100, collected_at=1200)
        self.collector.save_batch([], start=800, end=900, collected_at=1300)
        self.assertEqual(self.collector.status()["last_successful_end"], 1100)

    def test_retention_only_prunes_fibaro_event_rows(self):
        self.collector.save_batch(self.events, start=900, end=1100, collected_at=1200)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,payload_json,payload_sha256) "
                "VALUES('test','keep',1000,1200,'{}','hash')"
            )
        result = self.collector.prune(now=1200 + 31 * 86400)
        self.assertEqual(result["deleted"], 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 1)

    def test_rejects_unbounded_windows_and_invalid_limits(self):
        with self.assertRaises(ValueError):
            self.collector.save_batch([], start=1, end=1 + 8 * 86400, collected_at=1200)
        with self.assertRaises(ValueError):
            self.collector.read_events(limit=0)

    def test_requires_applied_shared_migrations(self):
        with tempfile.TemporaryDirectory() as other:
            empty = SQLiteStore(Path(other) / "empty.sqlite3", migrations_dir=self.root / "missing")
            with self.assertRaises(RuntimeError):
                FibaroHistoryCollector(empty)


    def test_snapshot_fetch_timestamp_does_not_create_false_state_transitions(self):
        first = self.collector.save_snapshot(
            {"fetched_at": "2026-11-26T08:00:00+00:00",
             "zones": [{"id": 7, "name": "Salon"}], "heads": []},
            captured_at=1000,
        )
        repeated = self.collector.save_snapshot(
            {"fetched_at": "2026-11-26T08:05:00+00:00",
             "zones": [{"id": 7, "name": "Salon"}], "heads": []},
            captured_at=1300,
        )
        history = self.collector.read_snapshots(start=900, end=1400)
        latest = self.collector.read_latest_snapshot()
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(repeated["status"], "duplicate")
        self.assertEqual(len(history), 1)
        self.assertEqual(latest["captured_at"], 1000)
        self.assertEqual(latest["last_observed_at"], 1300)

    def test_snapshot_reversion_is_recorded_as_a_new_historical_transition(self):
        snapshot_a = {"zones": [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}]}
        snapshot_b = {"zones": [{"id": 13, "name": "Sypialnia", "properties": {"devices": [2162]}}]}
        first = self.collector.save_snapshot(snapshot_a, captured_at=1000)
        middle = self.collector.save_snapshot(snapshot_b, captured_at=1100)
        reverted = self.collector.save_snapshot(snapshot_a, captured_at=1200)
        history = self.collector.read_snapshots(start=900, end=1300)
        self.assertEqual([row["captured_at"] for row in history], [1000, 1100, 1200])
        self.assertEqual([row["payload"]["zones"][0]["id"] for row in history], [7, 13, 7])
        self.assertEqual(first["status"], "inserted")
        self.assertEqual(middle["status"], "inserted")
        self.assertEqual(reverted["status"], "inserted")
        self.assertEqual(self.collector.read_latest_snapshot()["captured_at"], 1200)


if __name__ == "__main__":
    unittest.main()
