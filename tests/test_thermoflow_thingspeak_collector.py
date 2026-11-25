"""Regression tests for explicit ThingSpeak-to-SQLite ingestion."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_thingspeak_collector import ThingSpeakHistoryCollector


class ThingSpeakCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SQLiteStore(
            root / "thermoflow.sqlite3",
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = ThingSpeakHistoryCollector(self.store)
        self.start = 1795600800
        self.end = self.start + 3600
        self.feeds = [
            {"entry_id": 101, "created_at": "2026-11-25T10:00:00Z", "field1": "68.5"},
            {"entry_id": 102, "created_at": "2026-11-25T10:01:00Z", "field1": "69"},
        ]

    def tearDown(self):
        self.temp.cleanup()

    def channels(self, feeds=None):
        return {
            "temperatures_power": {
                "kind": "telemetry", "channel_id": "111", "feeds": self.feeds if feeds is None else feeds,
            },
            "refills": {
                "kind": "refills", "channel_id": "222",
                "feeds": [{"entry_id": 7, "created_at": "2026-11-25T10:02:00Z", "field1": "30"}],
            },
        }

    def test_saves_telemetry_and_refill_events_in_shared_schema(self):
        result = self.collector.save_batch(self.channels(), start=self.start, end=self.end, collected_at=self.end + 10)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["inserted"], 3)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM refill_events").fetchone()[0], 1)
        status = self.collector.status()
        self.assertEqual(status["counts"], {"telemetry_samples": 2, "refill_events": 1})
        self.assertEqual(status["cursors"]["thingspeak_history:temperatures_power"], str(self.end))

    def test_identical_replay_is_idempotent(self):
        self.collector.save_batch(self.channels(), start=self.start, end=self.end, collected_at=self.end + 10)
        result = self.collector.save_batch(self.channels(), start=self.start, end=self.end, collected_at=self.end + 20)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["duplicates"], 3)
        self.assertEqual(result["status"], "complete")

    def test_same_entry_id_with_changed_payload_is_conflict_and_does_not_overwrite(self):
        self.collector.save_batch(self.channels(), start=self.start, end=self.end, collected_at=self.end + 10)
        changed = self.channels()
        changed["temperatures_power"]["feeds"][0]["field1"] = "99"
        result = self.collector.save_batch(changed, start=self.start, end=self.end, collected_at=self.end + 20)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["status"], "partial")
        with self.store.connection() as db:
            row = db.execute("SELECT payload_json FROM telemetry_samples WHERE source_key='111:101'").fetchone()
        self.assertIn('"68.5"', row["payload_json"])
        self.assertEqual(
            self.collector.status()["cursors"]["thingspeak_history:temperatures_power"],
            str(self.end),
        )

    def test_invalid_feed_makes_run_partial_without_advancing_cursor(self):
        channels = self.channels([{"entry_id": 1, "created_at": "bad", "field1": "1"}])
        result = self.collector.save_batch(channels, start=self.start, end=self.end, collected_at=self.end + 10)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("thingspeak_history:temperatures_power", self.collector.status()["cursors"])

    def test_collect_once_uses_injected_fetcher_and_never_writes_upstream(self):
        calls = []
        def fetch(channel_id, start, end):
            calls.append((channel_id, start, end))
            return [{"entry_id": int(channel_id), "created_at": "2026-11-25T10:00:00Z", "field1": "1"}]
        channels = {"one": {"kind": "telemetry", "channel_id": "111"}}
        result = self.collector.collect_once(fetch, channels, start=self.start, end=self.end, collected_at=self.end + 1)
        self.assertEqual(calls, [("111", self.start, self.end)])
        self.assertEqual(result["inserted"], 1)

    def test_rejects_unbounded_windows_and_oversized_responses(self):
        with self.assertRaises(ValueError):
            self.collector.save_batch(self.channels(), start=self.start, end=self.start + 8 * 86400)
        too_many = [{"entry_id": i + 1, "created_at": "2026-11-25T10:00:00Z"} for i in range(8001)]
        with self.assertRaises(ValueError):
            self.collector.save_batch(
                {"one": {"kind": "telemetry", "channel_id": "1", "feeds": too_many}},
                start=self.start, end=self.end,
            )

    def test_schema_is_required_and_no_implicit_migration_runs(self):
        empty = SQLiteStore(Path(self.temp.name) / "empty.sqlite3")
        with self.assertRaises(RuntimeError):
            ThingSpeakHistoryCollector(empty)
        self.assertEqual(empty.schema_version(), 0)


if __name__ == "__main__":
    unittest.main()
