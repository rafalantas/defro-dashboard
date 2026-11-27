"""Local SQLite mirror serving ThingSpeak feeds.json range reads."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fuel_http
import thermoflow_local_feeds as local_feeds
from fuel_thingspeak_daily_provider import fetch_day
from thermoflow_store import SQLiteStore
from thermoflow_thingspeak_collector import _payload, _timestamp

ROOT = Path(__file__).resolve().parents[1]
CHANNEL = "3566081"
OTHER_CHANNEL = "3566084"
URL = f"https://api.thingspeak.com/channels/{CHANNEL}/feeds.json"
BASE = int(datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).timestamp())


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_feeds(first_epoch, count, step=300, first_entry=1):
    """Entries shaped exactly like a ThingSpeak response (string fields, JSON nulls)."""
    return [{
        "created_at": iso(first_epoch + i * step), "entry_id": first_entry + i,
        "field1": f"{40 + i % 7}.5", "field2": f"{5 + (i % 11) * 0.5}", "field3": f"{i % 20}.0",
        "field4": None, "field5": "21.0", "field6": "30.0", "field7": "45.0", "field8": f"{i % 9}.0",
    } for i in range(count)]


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database, migrations_dir=ROOT / "db" / "migrations")
        self.store.migrate()
        env = patch.dict(os.environ, {"THERMOFLOW_DB_PATH": str(self.database)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("THERMOFLOW_READ_SOURCE", None)
        os.environ.pop("THERMOFLOW_LOCAL_FEEDS_START", None)
        local_feeds.reset_state()

    def mirror(self, feeds, *, channel=CHANNEL, name="temperatures_power", cursor=None, legacy=False):
        """Store feeds the way the ThingSpeak collector does."""
        with self.store.connection() as db:
            for feed in feeds:
                payload, digest = _payload(feed)
                if legacy:    # old rollout rows kept the full feed in the payload
                    payload = json.dumps(feed, sort_keys=True, separators=(",", ":"))
                observed = _timestamp(feed["created_at"])
                db.execute(
                    "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,payload_json,"
                    "payload_sha256) VALUES(?,?,?,?,?,?)",
                    (f"thingspeak:{name}", f"{channel}:{feed['entry_id']}", observed, observed, payload, digest))
            last = cursor if cursor is not None else (_timestamp(feeds[-1]["created_at"]) if feeds else 1)
            db.execute(
                "INSERT OR REPLACE INTO collector_state(collector_name,cursor_value,updated_at,metadata_json) "
                "VALUES(?,?,?,'{}')", (f"thingspeak_history:{name}", str(last), last))

    def serve(self, params, *, now=None, url=URL):
        return local_feeds.try_serve(url, params, db_path=self.database, now=now)


class ShapeTests(MirrorCase):
    def test_round_trip_is_identical_to_the_thingspeak_response(self):
        feeds = make_feeds(BASE, 50)
        self.mirror(feeds)
        result = self.serve({"start": utc(BASE), "end": utc(BASE + 49 * 300), "results": 8000}, now=BASE + 20000)
        self.assertEqual(result["feeds"], feeds)                  # incl. string fields and nulls
        self.assertEqual(result["channel"]["id"], int(CHANNEL))

    def test_window_filters_inclusively_and_keeps_chronological_order(self):
        feeds = make_feeds(BASE, 20)
        self.mirror(feeds)
        result = self.serve({"start": utc(BASE + 5 * 300), "end": utc(BASE + 9 * 300), "results": 8000},
                            now=BASE + 20000)
        self.assertEqual([f["entry_id"] for f in result["feeds"]], [6, 7, 8, 9, 10])

    def test_naive_local_bounds_with_timezone_match_the_utc_equivalent(self):
        feeds = make_feeds(BASE, 40)
        self.mirror(feeds)
        warsaw = ZoneInfo("Europe/Warsaw")
        local = datetime.fromtimestamp(BASE + 300, warsaw).strftime("%Y-%m-%d %H:%M:%S")
        local_end = datetime.fromtimestamp(BASE + 10 * 300, warsaw).strftime("%Y-%m-%d %H:%M:%S")
        a = self.serve({"start": local, "end": local_end, "timezone": "Europe/Warsaw", "results": 8000},
                       now=BASE + 20000)
        b = self.serve({"start": utc(BASE + 300), "end": utc(BASE + 10 * 300), "results": 8000}, now=BASE + 20000)
        self.assertEqual(a, b)
        self.assertEqual(len(a["feeds"]), 10)

    def test_legacy_full_feed_rows_are_served_without_duplicated_keys(self):
        feeds = make_feeds(BASE, 5)
        self.mirror(feeds, legacy=True)
        result = self.serve({"start": utc(BASE), "end": utc(BASE + 4 * 300), "results": 8000}, now=BASE + 20000)
        self.assertEqual(result["feeds"], feeds)

    def test_channels_are_not_mixed(self):
        self.mirror(make_feeds(BASE, 5), channel=CHANNEL, name="temperatures_power")
        other = make_feeds(BASE, 3, first_entry=100)
        self.mirror(other, channel=OTHER_CHANNEL, name="valve_pumps_fuel")
        result = self.serve({"start": utc(BASE), "end": utc(BASE + 4 * 300), "results": 8000},
                            now=BASE + 20000, url=f"https://api.thingspeak.com/channels/{OTHER_CHANNEL}/feeds.json")
        self.assertEqual([f["entry_id"] for f in result["feeds"]], [100, 101, 102])


class FallbackTests(MirrorCase):
    def setUp(self):
        super().setUp()
        self.feeds = make_feeds(BASE, 40)
        self.mirror(self.feeds)
        self.params = {"start": utc(BASE), "end": utc(BASE + 39 * 300), "results": 8000}
        self.now = BASE + 20000

    def reason(self):
        return set(local_feeds.STATS["fallback"])

    def test_unsupported_or_incomplete_requests_use_thingspeak(self):
        for extra in ({"timescale": 10}, {"average": 10}, {"median": 10}, {"sum": 10}, {"round": 2}):
            self.assertIsNone(self.serve({**self.params, **extra}, now=self.now), extra)
        no_results = {k: v for k, v in self.params.items() if k != "results"}
        self.assertIsNone(self.serve(no_results, now=self.now))
        self.assertIsNone(self.serve({"results": 1}, now=self.now))                 # channel metadata probe
        for bad in (0, 8001):
            self.assertIsNone(self.serve({**self.params, "results": bad}, now=self.now))
        self.assertIsNone(self.serve({**self.params, "end": self.params["start"]}, now=self.now))
        self.assertIsNone(self.serve({**self.params, "start": "garbage"}, now=self.now))
        self.assertIsNone(self.serve({**self.params, "timezone": "Mars/Base"}, now=self.now))

    def test_other_urls_are_never_served(self):
        self.assertIsNone(self.serve(self.params, now=self.now, url="https://api.thingspeak.com/update.json"))
        self.assertIsNone(self.serve(self.params, now=self.now,
                                     url="https://api.thingspeak.com/channels/abc/feeds.json"))

    def test_rollback_switch_disables_local_reads(self):
        self.assertIsNotNone(self.serve(self.params, now=self.now))
        for value in ("thingspeak", "remote", "off", "false"):
            with patch.dict(os.environ, {"THERMOFLOW_READ_SOURCE": value}):
                self.assertIsNone(self.serve(self.params, now=self.now), value)

    def test_missing_database_and_unknown_channel(self):
        self.assertIsNone(local_feeds.try_serve(URL, self.params, db_path=Path(self.tempdir.name) / "none.sqlite3"))
        self.assertIsNone(self.serve(self.params, now=self.now,
                                     url="https://api.thingspeak.com/channels/999/feeds.json"))
        self.assertTrue({"no_database", "unknown_channel"} <= self.reason())

    def test_window_before_the_first_mirrored_sample_uses_thingspeak(self):
        early = {**self.params, "start": utc(BASE - 86400)}
        self.assertIsNone(self.serve(early, now=self.now))
        self.assertIn("before_local_history", self.reason())

    def test_operator_can_declare_the_mirror_complete_from_an_earlier_date(self):
        early = {**self.params, "start": utc(BASE - 86400)}
        with patch.dict(os.environ, {"THERMOFLOW_LOCAL_FEEDS_START": utc(BASE - 86400).replace(" ", "T")}):
            result = self.serve(early, now=self.now)
        self.assertEqual(len(result["feeds"]), 40)

    def test_stale_collector_uses_thingspeak_for_recent_windows_only(self):
        last = BASE + 39 * 300
        self.assertIsNone(self.serve({**self.params, "end": utc(last + 3600)}, now=last + 3600))   # mirror behind
        self.assertIn("collector_behind", self.reason())
        self.assertIsNotNone(self.serve(self.params, now=last + 7 * 86400))           # old window fully covered

    def test_missing_cursor_means_unknown_coverage(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM collector_state")
        self.assertIsNone(self.serve(self.params, now=self.now))

    def test_truncation_is_not_guessed(self):
        self.assertIsNone(self.serve({**self.params, "results": 10}, now=self.now))
        self.assertIn("truncated", self.reason())
        self.assertEqual(len(self.serve({**self.params, "results": 40}, now=self.now)["feeds"]), 40)

    def test_corrupt_row_falls_back_instead_of_raising(self):
        with self.store.connection() as db:
            db.execute("UPDATE telemetry_samples SET payload_json='not json' WHERE source_key=?", (f"{CHANNEL}:5",))
        self.assertIsNone(self.serve(self.params, now=self.now))
        self.assertIn("error", self.reason())

    def test_counters_track_local_and_fallback_use(self):
        self.serve(self.params, now=self.now)
        self.serve({**self.params, "timescale": 10}, now=self.now)
        self.assertEqual(local_feeds.STATS["local"], 1)
        self.assertEqual(local_feeds.STATS["fallback"]["not_a_range_read"], 1)


class FuelEngineIntegrationTests(MirrorCase):
    """The fuel engine's transport and its day analysis must not notice the difference."""

    def test_request_json_serves_locally_without_touching_the_network(self):
        feeds = make_feeds(BASE, 30)
        self.mirror(feeds, cursor=int(time.time()))
        params = {"start": utc(BASE), "end": utc(BASE + 29 * 300), "results": 8000}
        with patch.object(fuel_http, "urlopen", side_effect=AssertionError("network used")):
            result = fuel_http.request_json(URL, params)
        self.assertEqual(result["feeds"], feeds)

    def test_request_json_falls_back_to_thingspeak_when_not_servable(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"feeds": [{"entry_id": 1}]}'
        params = {"start": utc(BASE), "end": utc(BASE + 3600), "results": 8000, "timescale": 10}
        with patch.object(fuel_http, "urlopen", return_value=Response()) as opened:
            result = fuel_http.request_json(URL, params)
        self.assertEqual(result, {"feeds": [{"entry_id": 1}]})
        self.assertEqual(opened.call_count, 1)

    def test_post_and_non_feed_requests_never_use_the_mirror(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"ok": true}'
        self.mirror(make_feeds(BASE, 5), cursor=int(time.time()))
        params = {"start": utc(BASE), "end": utc(BASE + 600), "results": 8000}
        with patch.object(fuel_http, "urlopen", return_value=Response()) as opened:
            fuel_http.request_json(URL, params, form={"field1": "1"})
            fuel_http.request_json("https://api.open-meteo.com/v1/forecast", {"latitude": 1})
        self.assertEqual(opened.call_count, 2)

    def test_daily_analysis_is_identical_from_thingspeak_and_from_the_mirror(self):
        tz = ZoneInfo("Europe/Warsaw")
        feeds = make_feeds(BASE - 6 * 3600, 2 * 24 * 12, step=300)        # ~two days at 5 minutes
        self.mirror(feeds, cursor=int(time.time()))

        def fake_thingspeak(url, params=None, **_):
            lo = int(datetime.strptime(params["start"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp())
            hi = int(datetime.strptime(params["end"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp())
            picked = [f for f in feeds if lo <= _timestamp(f["created_at"]) <= hi]
            return {"channel": {"id": int(CHANNEL)}, "feeds": picked[:params["results"]]}

        common = dict(day=date(2026, 11, 19), tz=tz, channel_id=CHANNEL, read_key="", endpoint="https://api.thingspeak.com",
                      outdoor_field=1, power_field=8, hdd_base=15.5, max_gap_seconds=900,
                      parse_timestamp=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")))
        expected = fetch_day(request_json=fake_thingspeak, **common)
        with patch.object(fuel_http, "urlopen", side_effect=AssertionError("network used")):
            actual = fetch_day(request_json=fuel_http.request_json, **common)
        self.assertGreater(expected["samples"], 200)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
