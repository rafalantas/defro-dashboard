"""Regression tests for scheduled ThingSpeak history ingestion."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import thermoflow_thingspeak_scheduler as scheduler
from thermoflow_store import SQLiteStore


class ThingSpeakSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.now = 1795725500
        self.env = {
            "THINGSPEAK_CHANNEL_IDS": "111,222",
            "THINGSPEAK_READ_KEYS": "read-one,read-two",
            "THINGSPEAK_FIRE_CHANNEL_ID": "333",
            "THINGSPEAK_FIRE_READ_KEY": "fire-read",
            "THINGSPEAK_FUEL_CHANNEL_ID": "444",
            "THINGSPEAK_FUEL_READ_KEY": "fuel-read",
            "THERMOFLOW_THINGSPEAK_HISTORY_DAYS": "7",
            "THERMOFLOW_THINGSPEAK_REQUEST_INTERVAL_SECONDS": "0",
        }

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _local_epoch(value):
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=ZoneInfo("Europe/Warsaw")
        )
        return int(parsed.timestamp())

    def _request(self, calls):
        def request(url, params):
            channel_id = url.split("/channels/")[1].split("/")[0]
            calls.append((channel_id, dict(params)))
            stamp = self.now - 5 * 86400 - 60
            created = datetime.fromtimestamp(stamp, ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
            feed = {
                "entry_id": int(channel_id) * 100000 + stamp,
                "created_at": created,
                "field1": "65.5",
            }
            if channel_id == "444":
                feed.update(field1="30", field2="backfill test", field3="")
            return {"feeds": [feed]}
        return request

    def test_ingests_all_configured_channels_and_structured_refills(self):
        calls = []
        result = scheduler.run_once(
            str(self.path), now=self.now, env=self.env,
            request_json=self._request(calls), monotonic=lambda: 1.0,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(result["channels"]), {
            "temperatures_power", "valve_pumps_fuel", "fire", "fuel_refills_raw",
        })
        self.assertEqual(len(calls), 4)
        self.assertNotIn("api_key", result)
        self.assertEqual(result["telemetry"]["inserted"], 4)
        self.assertEqual(result["refills"]["inserted"], 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 4)
            refill = db.execute("SELECT event_key,source FROM refill_events").fetchone()
            self.assertEqual(refill["event_key"].split(":")[0:2], ["thingspeak", "fuel"])
            self.assertEqual(refill["source"], "thingspeak_fuel")
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM collection_runs WHERE collector_name LIKE 'thingspeak_history%'"
                ).fetchone()[0],
                2,
            )

    def test_second_cycle_resumes_from_persisted_cursor_with_small_overlap(self):
        calls = []
        first = scheduler.run_once(
            str(self.path), now=self.now, env=self.env,
            request_json=self._request(calls), monotonic=lambda: 1.0,
        )
        second = scheduler.run_once(
            str(self.path), now=self.now, env=self.env,
            request_json=self._request(calls), monotonic=lambda: 1.0,
        )
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(
            second["window"]["start"],
            first["window"]["end"] - scheduler.OVERLAP_SECONDS,
        )
        self.assertEqual(second["telemetry"]["duplicates"], 4)
        self.assertEqual(second["refills"]["unchanged"], 1)

    def test_missing_main_channel_configuration_blocks_without_writes(self):
        result = scheduler.run_once(
            str(self.path), now=self.now, env={"THINGSPEAK_CHANNEL_IDS": "111"},
            request_json=lambda *_: self.fail("network must not be called"),
        )
        self.assertEqual(result["status"], "blocked")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 0)

    def test_adaptive_fetch_splits_a_saturated_window(self):
        original_limit = scheduler.MAX_FEEDS_PER_CHANNEL
        scheduler.MAX_FEEDS_PER_CHANNEL = 2
        calls = []
        try:
            def request(url, params):
                start = self._local_epoch(params["start"])
                end = self._local_epoch(params["end"])
                calls.append((start, end))
                if end - start > 150:
                    return {"feeds": [{"entry_id": 1}, {"entry_id": 2}]}
                return {"feeds": [{"entry_id": start}]}
            limiter = scheduler._RequestLimiter(
                interval_seconds=0, wait=lambda _: None, monotonic=lambda: 1.0,
            )
            feeds = scheduler._fetch_channel_window(
                "111", "", 1795720000, 1795720400,
                channel_name="test", request_json=request, limiter=limiter,
            )
            self.assertGreater(len(calls), 1)
            self.assertGreater(len(feeds), 1)
        finally:
            scheduler.MAX_FEEDS_PER_CHANNEL = original_limit


if __name__ == "__main__":
    unittest.main()
