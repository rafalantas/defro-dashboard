"""Tests for refill-period AI training observations."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fuel_refill_ai import build_refill_period_rows
import fuel_ai


class RefillPeriodAiTests(unittest.TestCase):
    def test_one_observation_spans_multiple_days_without_daily_refills(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 3, 20, tzinfo=tz).isoformat(), "kg": 36},
        ]
        def features(start, end):
            return [
                {"fraction": 1.0, "hdd_lag": 10, "solar_mj": 2, "wind_max_kmh": 20},
                {"fraction": 1.0, "hdd_lag": 8, "solar_mj": 4, "wind_max_kmh": 30},
                {"fraction": 0.5, "hdd_lag": 6, "solar_mj": 1, "wind_max_kmh": 10},
            ]
        rows, meta = build_refill_period_rows(refills, features, max_days=120)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["period_days"], 2.5)
        self.assertAlmostEqual(rows[0]["consumed_kg"], 36)
        self.assertAlmostEqual(rows[0]["kg_counter"], 14.4)
        self.assertAlmostEqual(rows[0]["hdd_lag"], 8.4)
        self.assertEqual(rows[0]["source"], "refill_period")
        self.assertFalse(meta["daily_refills_required"])
        self.assertEqual(meta["periods_accepted"], 1)

    def test_distinct_refills_on_same_date_remain_distinct_ai_samples(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 20},
            {"ts": datetime(2026, 1, 1, 14, tzinfo=tz).isoformat(), "kg": 5},
            {"ts": datetime(2026, 1, 2, 8, tzinfo=tz).isoformat(), "kg": 12},
        ]
        def features(start, end):
            return [{"fraction": (end-start).total_seconds()/86400,
                     "hdd_lag": 8, "solar_mj": 2, "wind_max_kmh": 10}]
        rows, _ = build_refill_period_rows(refills, features, max_days=120)
        self.assertEqual(len(rows), 2)
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["rejected_by_reason"]["duplicate_day"], 0)
        self.assertEqual(result["rejected_days"], 0)

    def test_optional_telemetry_is_time_weighted_over_the_refill_period(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 3, 8, tzinfo=tz).isoformat(), "kg": 24},
        ]
        def features(start, end):
            base = {"fraction": 1.0, "hdd_lag": 8, "solar_mj": 2,
                    "wind_max_kmh": 10}
            first = dict(base, telemetry_return_c=50, telemetry_pump_floor_h=10)
            second = dict(base, telemetry_return_c=54, telemetry_pump_floor_h=14)
            return [first, second]
        rows, _ = build_refill_period_rows(refills, features, max_days=120)
        self.assertAlmostEqual(rows[0]["telemetry_return_c"], 52)
        self.assertAlmostEqual(rows[0]["telemetry_pump_floor_h"], 12)

    def test_partial_telemetry_keeps_independently_covered_features(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 3, 8, tzinfo=tz).isoformat(), "kg": 24},
        ]

        def features(start, end):
            base = {"fraction": 1.0, "hdd_lag": 8, "solar_mj": 2,
                    "wind_max_kmh": 10}
            first = dict(base, telemetry_return_c=50, telemetry_pump_floor_h=10)
            second = dict(base, telemetry_return_c=None, telemetry_pump_floor_h=14)
            return [first, second]

        rows, _ = build_refill_period_rows(refills, features, max_days=120)
        row = rows[0]
        self.assertAlmostEqual(row["telemetry_pump_floor_h"], 12)
        self.assertNotIn("telemetry_return_c", row)
        self.assertAlmostEqual(row["telemetry_feature_coverage"]["telemetry_return_c"], 0.5)
        self.assertAlmostEqual(row["telemetry_feature_coverage"]["telemetry_pump_floor_h"], 1.0)
        self.assertIn("telemetry_return_c", row["telemetry_missing_features"])
        self.assertNotIn("telemetry_pump_floor_h", row["telemetry_missing_features"])
        self.assertFalse(row["telemetry_complete"])

    def test_telemetry_feature_with_at_least_90_percent_coverage_is_retained(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 2, 8, tzinfo=tz).isoformat(), "kg": 12},
        ]
        def features(start, end):
            return [{
                "fraction": 1.0, "hdd_lag": 8, "solar_mj": 2, "wind_max_kmh": 10,
                "telemetry_return_c": 55, "telemetry_pump_floor_h": 10,
            }]
        rows, _ = build_refill_period_rows(refills, features, max_days=120)
        self.assertAlmostEqual(rows[0]["telemetry_return_c"], 55)
        self.assertAlmostEqual(rows[0]["telemetry_feature_coverage"]["telemetry_return_c"], 1.0)
        self.assertTrue(rows[0]["telemetry_complete"])

    def test_missing_weather_rejects_period_without_imputation(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 2, 8, tzinfo=tz).isoformat(), "kg": 12},
        ]
        rows, meta = build_refill_period_rows(
            refills,
            lambda start, end: [{"fraction": 1.0, "hdd_lag": 8,
                                 "solar_mj": None, "wind_max_kmh": 10}],
            max_days=120,
        )
        self.assertEqual(rows, [])
        self.assertEqual(meta["rejected_by_reason"]["missing_features"], 1)

    def test_invalid_and_too_long_periods_are_excluded(self):
        tz = ZoneInfo("Europe/Warsaw")
        refills = [
            {"ts": datetime(2026, 1, 1, 8, tzinfo=tz).isoformat(), "kg": 30},
            {"ts": datetime(2026, 1, 1, 7, tzinfo=tz).isoformat(), "kg": 10},
            {"ts": datetime(2026, 8, 1, 8, tzinfo=tz).isoformat(), "kg": 40},
        ]
        rows, meta = build_refill_period_rows(
            refills,
            lambda start, end: [{"fraction": (end-start).total_seconds()/86400,
                                 "hdd_lag": 8, "solar_mj": 2, "wind_max_kmh": 10}],
            max_days=120,
        )
        self.assertEqual(rows, [])
        self.assertEqual(meta["rejected_by_reason"]["invalid_duration"], 1)
        self.assertEqual(meta["rejected_by_reason"]["period_too_long"], 1)


if __name__ == "__main__":
    unittest.main()
