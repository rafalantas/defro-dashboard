from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from thermoflow_store import SQLiteStore
from thermoflow_fuel_event_store import (
    persist_refill_feeds,
    persist_weather_forecasts,
)


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FuelEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database)
        self.store.migrate()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_refill_and_void_feed_entries_are_archived_once(self):
        feeds = [
            {
                "entry_id": 10,
                "created_at": "2026-11-26T18:00:00Z",
                "field1": "30",
                "field2": "pellet",
                "field3": "",
            },
            {
                "entry_id": 11,
                "created_at": "2026-11-26T19:00:00Z",
                "field1": "",
                "field2": "",
                "field3": "10",
            },
            {
                "entry_id": 12,
                "created_at": "not-a-date",
                "field1": "15",
                "field2": "bad timestamp",
                "field3": "",
            },
        ]

        first = persist_refill_feeds(
            feeds, database_path=self.database, tz=ZoneInfo("Europe/Warsaw"),
            parse_timestamp=parse_timestamp,
        )
        second = persist_refill_feeds(
            feeds, database_path=self.database, tz=ZoneInfo("Europe/Warsaw"),
            parse_timestamp=parse_timestamp,
        )

        self.assertEqual(first["inserted"], 2)
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["unchanged"], 2)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT event_key,occurred_at,source,payload_json "
                "FROM refill_events ORDER BY occurred_at"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_key"], "thingspeak:fuel:10")
        self.assertEqual(rows[0]["source"], "thingspeak_fuel")
        void_payload = json.loads(rows[1]["payload_json"])
        self.assertEqual(void_payload["voids_entry_id"], 10)
        self.assertIsNone(void_payload["kg"])


    def test_historical_older_refills_are_inserted_after_newer_events_were_archived(self):
        newer = [{
            "entry_id": 900,
            "created_at": "2026-11-26T18:00:00Z",
            "field1": "30",
            "field2": "newer",
            "field3": "",
        }]
        older = [{
            "entry_id": 100,
            "created_at": "2026-11-20T18:00:00Z",
            "field1": "15",
            "field2": "older historical event",
            "field3": "",
        }]
        kwargs = {
            "database_path": self.database,
            "tz": ZoneInfo("Europe/Warsaw"),
            "parse_timestamp": parse_timestamp,
        }
        self.assertEqual(persist_refill_feeds(newer, **kwargs)["inserted"], 1)
        backfill = persist_refill_feeds(older, **kwargs)
        self.assertEqual(backfill["inserted"], 1)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT event_key FROM refill_events ORDER BY occurred_at"
            ).fetchall()
        self.assertEqual(
            [row["event_key"] for row in rows],
            ["thingspeak:fuel:100", "thingspeak:fuel:900"],
        )

    def test_weather_forecast_deduplicates_unchanged_daily_payload(self):
        rows = [{
            "day": "2026-11-27",
            "t_mean": 3.2,
            "solar_mj": 2.1,
            "wind_max_kmh": 12.0,
            "kg": 21.5,
        }]
        first = persist_weather_forecasts(
            rows, fetched_at=1795800000, database_path=self.database
        )
        unchanged = persist_weather_forecasts(
            rows, fetched_at=1795803600, database_path=self.database
        )
        changed = persist_weather_forecasts(
            [{**rows[0], "t_mean": 4.0}],
            fetched_at=1795807200,
            database_path=self.database,
        )

        self.assertEqual(first["inserted"], 1)
        self.assertEqual(unchanged["inserted"], 0)
        self.assertEqual(unchanged["unchanged"], 1)
        self.assertEqual(changed["inserted"], 1)
        with self.store.connection() as db:
            records = db.execute(
                "SELECT source,forecast_for,fetched_at,payload_json "
                "FROM weather_forecasts ORDER BY fetched_at"
            ).fetchall()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["source"], "open-meteo")
        self.assertNotEqual(records[0]["forecast_for"], 0)
        latest = json.loads(records[-1]["payload_json"])
        self.assertEqual(latest["temperature_mean_c"], 4.0)
        self.assertEqual(latest["estimate_semantics"], "fuel_model_prediction_not_measurement")

    def test_writers_refuse_to_create_a_second_database(self):
        missing = Path(self.tempdir.name) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            persist_refill_feeds(
                [], database_path=missing, tz=ZoneInfo("Europe/Warsaw"),
                parse_timestamp=parse_timestamp,
            )
        with self.assertRaises(FileNotFoundError):
            persist_weather_forecasts(
                [{"day": "2026-11-27", "t_mean": 3.0}],
                fetched_at=1795800000,
                database_path=missing,
            )


if __name__ == "__main__":
    unittest.main()
