"""Regression tests for extracted summary estimate and hopper calculations."""
import unittest
from datetime import datetime, timedelta, timezone

from fuel_summary_estimates import build_last7_estimate, build_last_refill_and_hopper


class SummaryEstimateTests(unittest.TestCase):
    epoch = datetime(2026, 11, 13, 18, 27, tzinfo=timezone.utc)

    @staticmethod
    def bounds(day):
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        return start, start + timedelta(days=1)

    def test_last7_filters_partial_days_and_keeps_burner_provisional_label(self):
        rows = [
            {"day": f"2026-11-{day:02d}", "burner_kwh": 10, "hdd": 5,
             "samples": samples, "t_mean": 4}
            for day, samples in [(14, 100), (15, 100), (16, 60)]
        ]
        result = build_last7_estimate(
            rows, None, {"new": {"provisional": True}}, {"collecting": False},
            self.epoch, self.bounds, lambda kwh, _moment: kwh * 2,
            lambda *_: (None, False, False), lambda *_: 0,
        )
        self.assertEqual(result["days"], 2)
        self.assertEqual(result["kg"], 40.0)
        self.assertEqual(result["source"], "moc palnika (szacunek)")
        self.assertEqual(result["kg_per_day"], 20.0)

    def test_last7_counter_replaces_burner_only_when_valid_and_not_at_floor(self):
        rows = [{"day": "2026-11-14", "burner_kwh": 10, "hdd": 5,
                 "samples": 100, "t_mean": 4}]
        result = build_last7_estimate(
            rows, {"base_kg_per_day": 1}, {"new": {}}, {"collecting": True},
            self.epoch, self.bounds, lambda *_: 20,
            lambda *_: (13.26, True, False), lambda _model, _temp: 2,
        )
        self.assertEqual(result["kg"], 13.3)
        self.assertEqual(result["source"], "licznik podajnika (szacunek)")
        self.assertEqual(result["model_kg"], 2.0)

    def test_last7_counter_floor_keeps_burner_estimate(self):
        rows = [{"day": "2026-11-14", "burner_kwh": 10, "hdd": 0,
                 "samples": 100, "t_mean": 4}]
        result = build_last7_estimate(
            rows, None, {"new": {}}, {"collecting": False}, self.epoch,
            self.bounds, lambda *_: 17, lambda *_: (12, True, True),
            lambda *_: 0,
        )
        self.assertEqual(result["kg"], 17)
        self.assertIsNone(result["kg_per_hdd"])

    def hopper(self, **overrides):
        args = {
            "now": datetime(2026, 11, 20, 12, tzinfo=timezone.utc),
            "refills": [{"ts": "2026-11-19T12:00:00+00:00", "kg": 30}],
            "counter": {"collecting": True, "message": "zbieranie danych"},
            "calibration": {"calibrated": False, "days": 2},
            "model": None, "recent_periods": [{"kg_per_day": 4}],
            "week": {"kg_per_day": 5}, "forecast": [], "daily_model": None,
            "known_temps": {}, "period_sums": lambda *_: (3, 0, 0, True),
            "counter_kg": lambda *_: (10, True, False),
            "burner_kg": lambda *_: 7, "counter_method": lambda *_: "licznik",
            "select_estimate": lambda **kw: (kw["counter_kg"], "licznik"),
            "days_until": lambda *_: None, "capacity": 100, "reserve_percent": 15,
        }
        args.update(overrides)
        return build_last_refill_and_hopper(**args)

    def test_hopper_counter_usage_reserve_math_and_provisional_flag(self):
        last, hopper = self.hopper()
        self.assertEqual(last["hours_ago"], 24.0)
        self.assertEqual(hopper["used_kg"], 10.0)
        self.assertEqual(hopper["remaining_kg"], 90.0)
        self.assertEqual(hopper["reserve_kg"], 15.0)
        self.assertEqual(hopper["to_reserve_kg"], 75.0)
        self.assertFalse(hopper["below_reserve"])
        self.assertTrue(hopper["counter_provisional"])
        self.assertEqual(hopper["days_to_reserve"], 15.0)
        self.assertEqual(hopper["days_source"], "średnie zużycie z ostatnich dni")

    def test_hopper_uses_weather_forecast_days_before_rate_fallback(self):
        _last, hopper = self.hopper(
            model={"base_kg_per_day": 1, "kg_per_hdd": 1},
            forecast=[("2026-11-21", 4)],
            days_until=lambda *_: 3.4,
        )
        self.assertEqual(hopper["days_to_reserve"], 3.4)
        self.assertEqual(hopper["days_source"], "prognoza pogody i model domu")

    def test_hopper_below_reserve_and_no_refills(self):
        _last, hopper = self.hopper(
            select_estimate=lambda **_kw: (90, "model"),
        )
        self.assertTrue(hopper["below_reserve"])
        self.assertEqual(hopper["remaining_percent"], 10)
        self.assertEqual(build_last_refill_and_hopper(
            datetime.now(timezone.utc), [], {}, {}, None, [], None, [], None, {},
            lambda *_: (0, 0, 0, True), lambda *_: (None, False, False),
            lambda *_: None, lambda *_: "", lambda **_: (None, None),
            lambda *_: None, 100, 15,
        ), (None, None))


if __name__ == "__main__":
    unittest.main()
