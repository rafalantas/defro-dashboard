"""Accuracy of the archived pellet forecast against counter-based actuals."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import api
from fuel_prediction_accuracy import (
    MIN_DAYS_FOR_VERDICT, accuracy_from_engine, build_accuracy, read_forecast_archive,
)
from thermoflow_fuel_event_store import persist_weather_forecasts
from thermoflow_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Warsaw")
TODAY = date(2026, 12, 10)


def start_of(day):
    return int(datetime.combine(day, time.min, tzinfo=TZ).timestamp())


def actual_rows(values, first=date(2026, 12, 1), provisional=()):
    return [{"day": (first + timedelta(days=i)).isoformat(), "kg_counter": kg,
             "estimate": (first + timedelta(days=i)) in provisional} for i, kg in enumerate(values)]


def archive_for(rows, forecasts, lead_hours=12):
    """One forecast per day issued ``lead_hours`` before the day began."""
    out = {}
    for row, kg in zip(rows, forecasts):
        day = date.fromisoformat(row["day"])
        s = start_of(day)
        out[s] = [(s - lead_hours * 3600, kg, 1.0)]
    return out


class AsIssuedTests(unittest.TestCase):
    def test_picks_the_forecast_that_was_in_force_for_each_lead_time(self):
        day = date(2026, 12, 5)
        s = start_of(day)
        archive = {s: [(s - 72 * 3600, 10.0, 0.0), (s - 30 * 3600, 11.0, 0.0),
                       (s - 12 * 3600, 12.0, 0.0), (s + 2 * 3600, 99.0, 0.0)]}   # last one issued after the day began
        result = build_accuracy([{"day": day.isoformat(), "kg_counter": 12.0}], archive, tz=TZ, today=TODAY, days=30)
        forecast = result["days"][0]["forecast_kg"]
        self.assertEqual(forecast, {"day_ahead": 12.0, "24h": 11.0, "48h": 10.0})

    def test_a_forecast_issued_after_the_day_started_is_never_used(self):
        day = date(2026, 12, 5)
        s = start_of(day)
        result = build_accuracy([{"day": day.isoformat(), "kg_counter": 12.0}],
                                {s: [(s + 3600, 12.0, 0.0)]}, tz=TZ, today=TODAY, days=30)
        self.assertEqual(result["days"][0]["forecast_kg"], {"day_ahead": None, "24h": None, "48h": None})
        self.assertEqual(result["bands"]["day_ahead"]["days"], 0)

    def test_a_later_revision_replaces_an_earlier_one_only_within_the_lead_window(self):
        day = date(2026, 12, 5)
        s = start_of(day)
        archive = {s: [(s - 40 * 3600, 8.0, 0.0), (s - 20 * 3600, 9.0, 0.0), (s - 2 * 3600, 10.0, 0.0)]}
        forecast = build_accuracy([{"day": day.isoformat(), "kg_counter": 9.5}], archive,
                                  tz=TZ, today=TODAY, days=30)["days"][0]["forecast_kg"]
        self.assertEqual(forecast, {"day_ahead": 10.0, "24h": 8.0, "48h": None})


class ScoringTests(unittest.TestCase):
    def test_error_mae_and_bias(self):
        rows = actual_rows([10, 12, 8, 11])
        archive = archive_for(rows, [11, 11, 10, 9])           # errors +1, -1, +2, -2
        band = build_accuracy(rows, archive, tz=TZ, today=TODAY, days=30)["bands"]["day_ahead"]
        self.assertEqual(band["days"], 4)
        self.assertEqual(band["mae_kg"], 1.5)
        self.assertEqual(band["bias_kg"], 0.0)
        self.assertEqual(band["within_2kg_pct"], 100.0)
        self.assertEqual(band["assessment"], "collecting")

    def test_provisional_days_are_listed_but_not_scored(self):
        rows = actual_rows([10, 12, 8, 11], provisional={date(2026, 12, 2)})
        result = build_accuracy(rows, archive_for(rows, [10, 99, 8, 11]), tz=TZ, today=TODAY, days=30)
        self.assertEqual(result["bands"]["day_ahead"]["mae_kg"], 0.0)       # the 99 kg day is ignored
        self.assertEqual(result["bands"]["day_ahead"]["days"], 3)
        self.assertEqual(result["coverage"]["provisional_days_excluded"], 1)
        self.assertTrue(result["days"][1]["provisional"])

    def test_perfect_model_beats_both_naive_baselines(self):
        values = [8, 12, 9, 14, 7, 13, 10, 15, 8, 12]
        rows = actual_rows(values)
        band = build_accuracy(rows, archive_for(rows, values), tz=TZ, today=TODAY, days=30)["bands"]["day_ahead"]
        self.assertGreaterEqual(band["days"], MIN_DAYS_FOR_VERDICT)
        self.assertEqual(band["mae_kg"], 0.0)
        self.assertEqual(band["skill_vs"]["yesterday"], 1.0)
        self.assertEqual(band["skill_vs"]["mean7"], 1.0)
        self.assertEqual(band["assessment"], "better_than_naive")

    def test_flat_wrong_model_is_worse_than_naive_and_says_so(self):
        values = [8, 12, 9, 14, 7, 13, 10, 15, 8, 12]
        rows = actual_rows(values)
        result = build_accuracy(rows, archive_for(rows, [30] * len(values)), tz=TZ, today=TODAY, days=30)
        band = result["bands"]["day_ahead"]
        self.assertEqual(band["assessment"], "worse_than_naive")
        self.assertIn("gorszy", result["summary"])

    def test_baselines_are_scored_on_the_same_days_as_the_model(self):
        values = [8, 12, 9, 14, 7, 13, 10, 15, 8, 12]
        rows = actual_rows(values)
        archive = archive_for(rows, values)
        for key in list(archive)[:4]:                      # no forecast for the first four days
            archive.pop(key)
        band = build_accuracy(rows, archive, tz=TZ, today=TODAY, days=30)["bands"]["day_ahead"]
        yesterday_errors = [abs(values[i - 1] - values[i]) for i in range(4, 9)]   # 10th value is "today"
        self.assertEqual(band["days"], 5)
        self.assertEqual(band["baseline_mae_kg"]["yesterday"], round(sum(yesterday_errors) / 5, 2))

    def test_too_little_data_asks_for_patience_instead_of_a_verdict(self):
        rows = actual_rows([10, 12, 9])
        result = build_accuracy(rows, archive_for(rows, [10, 12, 9]), tz=TZ, today=TODAY, days=30)
        self.assertIn("Za wcześnie", result["summary"])
        self.assertIn("3 z 7", result["summary"])

    def test_summary_uses_polish_decimal_comma(self):
        values = [8, 12, 9, 14, 7, 13, 10, 15, 8, 12]
        rows = actual_rows(values)
        text = build_accuracy(rows, archive_for(rows, [v + 1.5 for v in values]), tz=TZ, today=TODAY, days=30)["summary"]
        self.assertIn("1,5 kg", text)
        self.assertNotIn("1.5", text)

    def test_unusable_rows_and_empty_inputs_do_not_break_the_report(self):
        rows = [{"day": "garbage", "kg_counter": 5}, {"day": "2026-12-03", "kg_counter": None},
                {"day": TODAY.isoformat(), "kg_counter": 9}, "not a dict", {"day": "2026-12-02", "kg_counter": "7.5"}]
        result = build_accuracy(rows, {}, tz=TZ, today=TODAY, days=30)
        self.assertEqual([d["day"] for d in result["days"]], ["2026-12-02"])        # today is not "completed"
        empty = build_accuracy([], {}, tz=TZ, today=TODAY, days=30)
        self.assertEqual(empty["bands"]["day_ahead"]["days"], 0)
        self.assertTrue(empty["safety"]["read_only"])
        self.assertIsNone(build_accuracy(None, {}, tz=TZ, today=TODAY, days=30)["bands"]["day_ahead"]["mae_kg"])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database, migrations_dir=ROOT / "db" / "migrations")
        self.store.migrate()

    def store_forecast(self, day, kg, fetched_at):
        persist_weather_forecasts(
            [{"day": day.isoformat(), "t_mean": 2.0, "solar_mj": 3.0, "wind_max_kmh": 20.0, "kg": kg}],
            fetched_at=fetched_at, database_path=self.database, timezone_name="Europe/Warsaw")

    def test_reads_what_the_fuel_engine_actually_archives_in_order_of_issue(self):
        day = date(2026, 12, 5)
        s = start_of(day)
        self.store_forecast(day, 9.0, s - 50 * 3600)
        self.store_forecast(day, 9.0, s - 40 * 3600)           # unchanged: the engine skips it
        self.store_forecast(day, 11.0, s - 10 * 3600)
        archive = read_forecast_archive(self.database, start=s - 86400, end=s + 86400)
        self.assertEqual([(f, kg) for f, kg, _ in archive[s]], [(s - 50 * 3600, 9.0), (s - 10 * 3600, 11.0)])
        result = build_accuracy([{"day": day.isoformat(), "kg_counter": 10.0}], archive, tz=TZ, today=TODAY, days=30)
        self.assertEqual(result["days"][0]["forecast_kg"]["day_ahead"], 11.0)
        self.assertEqual(result["days"][0]["forecast_kg"]["48h"], 9.0)

    def test_other_sources_rows_without_an_estimate_and_corrupt_payloads_are_ignored(self):
        day = date(2026, 12, 5)
        s = start_of(day)
        with self.store.connection() as db:
            for hours, (source, payload) in enumerate((("other", '{"estimated_pellet_kg_day": 5}'),
                                                       ("open-meteo", '{"temperature_mean_c": 1}'),
                                                       ("open-meteo", "not json"),
                                                       ("open-meteo", '{"estimated_pellet_kg_day": 7.5, "temperature_mean_c": 1}')), 1):
                db.execute("INSERT INTO weather_forecasts(source,forecast_for,fetched_at,payload_json,payload_sha256)"
                           " VALUES(?,?,?,?,?)", (source, s, s - 3600 * hours, payload, payload))
        archive = read_forecast_archive(self.database, start=s, end=s)
        self.assertEqual([kg for _, kg, _ in archive[s]], [7.5])

    def test_missing_database_is_an_empty_archive(self):
        self.assertEqual(read_forecast_archive(Path(self.tempdir.name) / "none.sqlite3", start=0, end=10**10), {})


class FakeEngine:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def handle(self, method, path, query, headers, body):
        self.calls += 1
        self.last = (method, path, query)
        return self.result


class EngineGlueTests(ArchiveTests):
    def test_combines_engine_actuals_with_the_archive(self):
        day = date(2026, 12, 8)
        s = start_of(day)
        self.store_forecast(day, 10.0, s - 6 * 3600)
        engine = FakeEngine((200, {"days": [{"day": day.isoformat(), "kg_counter": 11.0}]}))
        now = datetime(2026, 12, 10, 12, 0, tzinfo=TZ)
        status, payload = accuracy_from_engine(engine, 14, database_path=self.database, now=now)
        self.assertEqual(status, 200)
        self.assertEqual(payload["days"][0]["error_kg"]["day_ahead"], -1.0)
        self.assertEqual(engine.last, ("GET", "/api/fuel/daily", {"days": ["14"]}))

    def test_validates_days_and_passes_engine_failures_through(self):
        engine = FakeEngine(None)
        self.assertEqual(accuracy_from_engine(engine, 1, database_path=self.database)[0], 400)
        self.assertEqual(accuracy_from_engine(engine, 999, database_path=self.database)[0], 400)
        self.assertEqual(engine.calls, 0)
        self.assertEqual(accuracy_from_engine(engine, 30, database_path=self.database)[0], 502)
        failing = FakeEngine((503, {"error": "ThingSpeak niedostępny"}))
        self.assertEqual(accuracy_from_engine(failing, 30, database_path=self.database),
                         (503, {"error": "ThingSpeak niedostępny"}))


class RouteTests(unittest.TestCase):
    def setUp(self):
        api._FUEL_ACCURACY_CACHE.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        SQLiteStore(database, migrations_dir=ROOT / "db" / "migrations").migrate()
        env = patch.dict("os.environ", {"THERMOFLOW_DB_PATH": str(database)})
        env.start()
        self.addCleanup(env.stop)
        self.engine = FakeEngine((200, {"days": [{"day": "2026-12-02", "kg_counter": 9.0}]}))
        patcher = patch.object(api, "_load_fuel_engine", return_value=self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_endpoint_returns_the_report_and_caches_it(self):
        status, payload = self.get("/api/fuel/accuracy?days=10")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], "thermoflow-fuel-prediction-accuracy/1")
        self.get("/api/fuel/accuracy?days=10")
        self.assertEqual(self.engine.calls, 1)                     # second answer came from the cache

    def test_bad_days_are_rejected_without_calling_the_engine(self):
        self.assertEqual(self.get("/api/fuel/accuracy?days=abc")[0], 400)
        self.assertEqual(self.get("/api/fuel/accuracy?days=2")[0], 400)
        self.assertEqual(self.engine.calls, 0)


if __name__ == "__main__":
    unittest.main()
