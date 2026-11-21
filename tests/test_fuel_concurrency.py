"""Concurrency tests for ThermoFlow 2.0 caches, prefetch and lock scopes.

Run: python -m unittest tests/test_fuel_concurrency.py
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fuel_errors import FuelError
from fuel_runtime import FuelCacheState, SingleFlightCache, StageTimer
from fuel_stats_service import build_stats
from fuel_weather_provider import openmeteo_mean

TZ = ZoneInfo("Europe/Warsaw")


def run_threads(count, target):
    barrier = threading.Barrier(count)
    results, errors = [None] * count, []

    def worker(index):
        barrier.wait()
        try:
            results[index] = target(index)
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("possible deadlock: worker did not finish in 10 s")
    if errors:
        raise errors[0]
    return results


class SingleFlightTests(unittest.TestCase):
    def test_refresh_runs_once_for_concurrent_callers(self):
        cache, calls = SingleFlightCache(), []

        def refresh():
            calls.append(1)
            time.sleep(0.05)
            return "value"

        results = run_threads(8, lambda i: cache.run("key", refresh))
        self.assertEqual(calls, [1])
        self.assertEqual(results, ["value"] * 8)

    def test_error_reaches_every_waiter_and_key_is_released(self):
        cache = SingleFlightCache()

        def fail():
            time.sleep(0.05)
            raise FuelError(502, "down")

        def call(_index):
            try:
                cache.run("key", fail)
            except FuelError as error:
                return error.message

        self.assertEqual(run_threads(4, call), ["down"] * 4)
        self.assertEqual(cache.run("key", lambda: "fresh"), "fresh")

    def test_different_keys_refresh_in_parallel(self):
        cache = SingleFlightCache()
        started = time.perf_counter()
        run_threads(4, lambda i: cache.run(i, lambda: time.sleep(0.2)))
        self.assertLess(time.perf_counter() - started, 0.6)


class StageTimerTests(unittest.TestCase):
    def test_accumulates_from_many_threads(self):
        timer = StageTimer()

        def work(_index):
            with timer.stage("http"):
                time.sleep(0.01)

        run_threads(8, work)
        self.assertGreaterEqual(timer.snapshot()["http"], 0.07)

    def test_cache_state_keeps_legacy_lock_and_fields(self):
        state = FuelCacheState()
        self.assertIsNotNone(state.lock)
        self.assertEqual(state.offset, {"at": 0.0, "data": None})
        self.assertIn("temps_start", state.cache_locks)


class OpenMeteoTodayRaceTests(unittest.TestCase):
    """Regression: concurrent callers for today must not lose the value.

    Before the fix, thread A filled the shared cache, thread B filled it too,
    A popped the key and B's ``cache.pop`` then returned ``None`` - today's
    temperature silently disappeared under concurrency.
    """

    def test_concurrent_today_reads_all_return_value(self):
        today = date(2026, 11, 21)
        fetched = threading.Barrier(6, timeout=5)
        stored = threading.Barrier(6, timeout=5)

        class InterleavingCache(dict):
            """Every thread stores its value before any thread pops it."""
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                stored.wait()

        cache = InterleavingCache()

        def request_json(url, params):
            fetched.wait()       # all callers missed the cache before any stores
            return {"daily": {"time": [today.isoformat()], "temperature_2m_mean": [3.5]}}

        def call(_index):
            return openmeteo_mean(
                day=today, latitude="52", longitude="23", timezone=TZ,
                archive_url="a", forecast_url="f", cache=cache,
                request_json=request_json, fuel_error=FuelError,
                now=lambda: datetime(2026, 11, 21, 12, tzinfo=TZ),
            )

        self.assertEqual(run_threads(6, call), [3.5] * 6)
        self.assertNotIn(today.isoformat(), cache)   # today still not frozen

    def test_single_thread_behaviour_is_unchanged(self):
        today, cache = date(2026, 11, 21), {}
        common = dict(latitude="52", longitude="23", timezone=TZ, archive_url="a",
                      forecast_url="f", cache=cache, fuel_error=FuelError,
                      now=lambda: datetime(2026, 11, 21, 12, tzinfo=TZ))
        payload = {"daily": {"time": ["2026-11-20", "2026-11-21"],
                             "temperature_2m_mean": [1.0, 2.0]}}
        self.assertEqual(openmeteo_mean(day=today, request_json=lambda u, p: payload, **common), 2.0)
        self.assertEqual(cache, {"2026-11-20": 1.0})
        self.assertEqual(openmeteo_mean(day=date(2026, 11, 20), request_json=None, **common), 1.0)


class StatsLockScopeTests(unittest.TestCase):
    def test_past_days_warm_outside_lock_today_inside(self):
        lock = threading.Lock()
        seen = {}

        def prefetch(day):
            seen[day] = lock.locked()

        deps = {
            "max_days": 120,
            "now": lambda: datetime(2026, 11, 20, 12, tzinfo=TZ),
            "lock": lock,
            "prefetch_daily": prefetch,
            "get_daily": lambda day: {"day": day.isoformat(), "burner_kwh": None, "hdd": None},
            "refills": lambda: [],
            "fit_model": lambda values: None,
            "periods": lambda values: [],
            "calibration": lambda values: {"new": {"provisional": False}},
            "day_bounds": lambda day: (datetime(day.year, day.month, day.day, tzinfo=TZ),) * 2,
            "pellet_kwh_per_kg": 4.8, "boiler_eff": 0.88,
            "burner_factor": lambda calibration, moment: 1.0,
            "om_offset": lambda: {"offset": 0.0},
            "active_base": lambda: 15.0, "hdd_base": 15.0, "hdd_base_fixed": None,
            "bag_kg": 15.0,
            "counter_calibration": lambda refills: {},
            "counter_epoch": datetime(2026, 1, 1, tzinfo=TZ),
            "fuel_error": FuelError,
        }
        build_stats({"days": ["3"]}, deps)
        today = date(2026, 11, 20)
        self.assertEqual(seen, {today - timedelta(days=2): False,
                                today - timedelta(days=1): False,
                                today: True})


class RuntimeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.rt = importlib.import_module("thermoflow2_runtime")
        self.saved = {name: getattr(self.rt, name) for name in (
            "_request_json", "_get_daily_raw", "_openmeteo_mean", "_counter_points",
            "TS_DATA_START", "TS_CHANNEL_TEMPS", "FUEL_LAT", "FUEL_LON", "_weather")}
        self.rt._temps_start.clear()
        self.rt._offset_cache.update(at=0.0, data=None)
        self.rt._forecast_cache.update(at=0.0, data=None)

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(self.rt, name, value)
        self.rt._temps_start.clear()
        self.rt._offset_cache.update(at=0.0, data=None)
        self.rt._forecast_cache.update(at=0.0, data=None)

    def test_temps_data_start_detected_once(self):
        rt, calls = self.rt, []
        rt.TS_DATA_START, rt.TS_CHANNEL_TEMPS = "", "1"

        def request_json(url, params=None, **kwargs):
            calls.append(url)
            time.sleep(0.05)
            return {"channel": {"created_at": "2026-10-01T00:00:00Z"}, "feeds": []}

        rt._request_json = request_json
        results = run_threads(8, lambda i: rt._temps_data_start())
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(rt._temps_start), 1)
        self.assertLessEqual(len(calls), 2)   # detection may use up to two calls once

    def test_om_offset_computed_once_and_identical(self):
        rt, scans = self.rt, []
        today = datetime.now(rt.TZ).date()
        rt.TS_DATA_START = (today - timedelta(days=40)).isoformat()
        rt.FUEL_LAT, rt.FUEL_LON = "52", "23"

        def daily_raw(day):
            if day == today - timedelta(days=40):
                scans.append(1)
                time.sleep(0.05)
            return {"source": "thingspeak", "t_mean": 5.0, "samples": 1440}

        rt._get_daily_raw = daily_raw
        rt._openmeteo_mean = lambda day: 4.0
        results = run_threads(6, lambda i: rt._om_offset())
        self.assertEqual(scans, [1])
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["offset"], 1.0)

    def test_offset_refresh_does_not_deadlock_with_shared_lock_holder(self):
        """Summary holds _lock while calling _om_offset; a concurrent forecast
        refresh calls _om_offset without _lock. Neither path may wait on the other."""
        rt = self.rt
        today = datetime.now(rt.TZ).date()
        rt.TS_DATA_START = (today - timedelta(days=5)).isoformat()
        rt.FUEL_LAT, rt.FUEL_LON = "52", "23"
        rt._get_daily_raw = lambda day: (time.sleep(0.02), {"source": "open-meteo"})[1]
        rt._openmeteo_mean = lambda day: 4.0
        rt._weather = lambda day: {}
        rt._request_json = lambda url, params=None, **kw: {
            "daily": {"time": [today.isoformat()], "temperature_2m_mean": [2.0]}}

        def call(index):
            if index % 2:
                with rt._lock:
                    return rt._om_offset()["offset"]
            return rt._forecast()

        run_threads(6, call)   # run_threads fails on a 10 s hang

    def test_prefetch_deduplicates_same_day_across_concurrent_prefetches(self):
        rt, calls = self.rt, {}
        today = datetime.now(rt.TZ).date()
        rt.TS_CHANNEL_TEMPS, rt.TS_DATA_START = "1", (today - timedelta(days=3)).isoformat()
        guard = threading.Lock()

        def daily_raw(day):
            with guard:
                calls[day] = calls.get(day, 0) + 1
            time.sleep(0.1)
            return {}

        rt._get_daily_raw = daily_raw
        rt._counter_points = lambda day: []
        run_threads(3, lambda i: rt._prefetch(days=3))
        self.assertEqual(len(calls), 4)
        # Without dedup each day would be fetched 3x concurrently.
        self.assertLess(sum(calls.values()), 3 * 4)

    def test_http_stage_timing_is_recorded(self):
        rt = self.rt
        rt._raw_request_json, saved = (lambda url, params=None, **kw: {}), rt._raw_request_json
        try:
            rt._timed_request_json("https://api.open-meteo.com/v1/forecast", {})
        finally:
            rt._raw_request_json = saved
        self.assertIn("http:api.open-meteo.com", rt.stage_timings())


if __name__ == "__main__":
    unittest.main()
