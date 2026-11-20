"""Regression tests for summary calculation orchestration."""
import threading
import unittest
from unittest.mock import patch

import fuel
from datetime import datetime
from zoneinfo import ZoneInfo

from fuel_errors import FuelError
from fuel_summary_service import build_summary


class SummaryServiceTests(unittest.TestCase):
    def dependencies(self, **overrides):
        now = datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("Europe/Warsaw"))
        cache = {}
        calls = []
        def build_last_refill(*args):
            hopper_inputs = args[10]
            values = hopper_inputs(now, now, {"status": "ok"}, {"calibrated": True})
            calls.append(("hopper_inputs", values))
            return {"kg": 4.0}, {"percent": 75.0}
        deps = {
            "now": lambda: now,
            "lock": threading.RLock(),
            "refills": lambda: [{"id": 1}],
            "fit_model": lambda refills: {"base_kg_per_day": 0.2},
            "calibration_inputs": lambda refills: (
                [{"kg": 5}], {"calibrated": True}, {"status": "ok"}
            ),
            "get_daily": lambda day: {
                "day": day.isoformat(), "t_mean": 5.0, "hdd": 9.0,
                "burner_kwh": 8.0,
            },
            "om_offset": lambda: {"offset": 0.0},
            "forecast": lambda: [],
            "daily_rows": lambda: ([{"day": "2026-11-19"}], {}),
            "fit_daily_model": lambda rows: {"status": "ready"},
            "fuel_error": FuelError,
            "ai_evaluate": lambda rows: {"status": "shadow"},
            "refill_ai_rows": lambda refills: ([], {"source": "refill_periods", "daily_refills_required": False}),
            "pellet_kwh_per_kg": 4.8,
            "boiler_eff": 0.88,
            "burner_factor": lambda calibration, moment: 1.0,
            "counter_kg": lambda start, end, counter: (2.0, True, False),
            "counter_epoch": now,
            "day_bounds": lambda day: (now, now),
            "predict_kg_per_day": lambda model, temperature: 1.0,
            "build_last7": lambda *args: {"kg": 7.0},
            "build_refills_based": lambda periods, model: {"kg_per_hdd": 0.5},
            "period_sums": lambda start, end: (5.0, 10.0, 0, 0),
            "burner_kg": lambda start, end, calibration: 3.0,
            "build_last_refill_and_hopper": build_last_refill,
            "counter_method": lambda counter: "counter",
            "select_usage_estimate": lambda *args: {"kg": 1.0},
            "days_until": lambda *args: 4,
            "hopper_kg": 228.0,
            "hopper_reserve_percent": 15.0,
            "planning_forecast": lambda *args: {"status": "ready"},
            "telemetry_state": lambda rows: {"status": "available"},
            "active_base": lambda: 14.0,
            "hdd_base_fixed": None,
            "weather": lambda day: {"solar_mj": 2.0, "wind_max_kmh": 8.0},
            "rate_for": lambda *args: 1.5,
            "summary_cache": cache,
            "monotonic": lambda: 123.0,
        }
        deps.update(overrides)
        return deps, cache, calls

    def test_summary_calibration_preserves_legacy_last_seven_refills(self):
        refills = [{"id": index} for index in range(10)]
        calls = {}
        recent_periods = [{"period": "recent"}]
        calibration = {"scope": "recent"}
        counter = {"scope": "recent"}

        def periods(selected):
            calls["period_refill_ids"] = [row["id"] for row in selected]
            return recent_periods

        def calibrate(selected):
            calls["calibration_periods"] = selected
            return calibration

        def calibrate_counter(selected):
            calls["counter_refill_ids"] = [row["id"] for row in selected]
            return counter

        with patch.object(fuel, "_periods", side_effect=periods), \
             patch.object(fuel, "_calibration", side_effect=calibrate), \
             patch.object(fuel, "_counter_calibration", side_effect=calibrate_counter):
            result = fuel._summary_calibration_inputs(refills)

        self.assertEqual(calls["period_refill_ids"], [3, 4, 5, 6, 7, 8, 9])
        self.assertIs(calls["calibration_periods"], recent_periods)
        self.assertEqual(calls["counter_refill_ids"], [3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(result, (recent_periods, calibration, counter))

    def test_summary_payload_and_cache_contract(self):
        deps, cache, calls = self.dependencies()
        result = build_summary(deps)
        self.assertEqual(
            set(result),
            {"configured", "telemetry", "model", "hdd_base", "hdd_base_fixed",
             "om_offset", "daily_model", "ai_shadow", "planning", "forecast",
             "calibration", "counter_calibration", "last7", "refills_based",
             "last_refill", "hopper", "fetched_at"},
        )
        self.assertTrue(result["configured"])
        self.assertEqual(result["ai_shadow"]["status"], "shadow")
        self.assertEqual(cache, {"at": 123.0, "data": result})
        self.assertEqual(calls[0][0], "hopper_inputs")
        self.assertEqual(calls[0][1], (5.0, 2.0, True, False, 3.0))


    def test_forecast_weather_is_loaded_once_per_day(self):
        from datetime import date, timedelta
        deps, _, _ = self.dependencies()
        day = date(2026, 11, 21)
        weather_calls = []
        deps["forecast"] = lambda: [(day, 4.5), (day + timedelta(days=1), 3.0)]
        deps["weather"] = lambda current: (
            weather_calls.append(current) or {"solar_mj": 2.0, "wind_max_kmh": 8.0}
        )
        result = build_summary(deps)
        self.assertEqual(weather_calls, [day, day + timedelta(days=1)])
        self.assertEqual(len(result["forecast"]), 2)
        self.assertEqual(result["forecast"][0]["solar_mj"], 2.0)
        self.assertEqual(result["forecast"][1]["wind_max_kmh"], 8.0)

    def test_daily_counter_failure_does_not_block_refill_period_ai(self):
        def fail():
            raise FuelError(503, "daily counter telemetry unavailable")
        ai_calls = []
        deps, _, _ = self.dependencies(
            daily_rows=fail,
            refill_ai_rows=lambda refills: (
                [{"day": "2026-11-19", "kg_counter": 2.0}],
                {"source": "refill_periods", "periods_accepted": 1,
                 "daily_refills_required": False},
            ),
            ai_evaluate=lambda rows: (ai_calls.append(rows) or {"status": "shadow"}),
        )
        result = build_summary(deps)
        self.assertEqual(result["ai_shadow"]["status"], "shadow")
        self.assertEqual(result["ai_shadow"]["data_source"]["periods_accepted"], 1)
        self.assertEqual(len(ai_calls[0]), 1)
        self.assertIsNone(result["daily_model"])
        self.assertEqual(result["telemetry"], {"status": "available"})

    def test_ai_failure_is_shadow_only(self):
        def fail(_rows):
            raise RuntimeError("experimental model failure")
        deps, _, _ = self.dependencies(ai_evaluate=fail)
        result = build_summary(deps)
        self.assertEqual(result["ai_shadow"]["status"], "unavailable")
        self.assertEqual(
            result["ai_shadow"]["message"],
            "Model eksperymentalny chwilowo niedostępny",
        )
        self.assertEqual(
            result["ai_shadow"]["data_source"]["source"], "refill_periods"
        )
        self.assertEqual(result["last7"], {"kg": 7.0})


if __name__ == "__main__":
    unittest.main()
