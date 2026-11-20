"""Tests for refill-period AI training observations."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fuel_refill_ai import build_refill_period_rows


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
