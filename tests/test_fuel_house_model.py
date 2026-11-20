"""Regression tests for the extracted refill-based house model."""
import unittest
from datetime import datetime, timedelta, timezone

from fuel_house_model import (
    active_balance_temperature, build_training_rows, fit_house_model, fit_from_refills,
)


class HouseModelTests(unittest.TestCase):
    def setUp(self):
        self.refills = [
            {"ts": "2026-10-01T00:00:00+00:00", "kg": 0},
            {"ts": "2026-10-05T00:00:00+00:00", "kg": 80},
            {"ts": "2026-10-10T00:00:00+00:00", "kg": 100},
        ]

    def test_training_rows_preserve_period_duration_and_missing_day_rules(self):
        calls = []
        def temps(start, end):
            calls.append((start, end))
            if start.day == 5:
                return [(1.0, 8.0)], 1
            return [(0.5, 7.0), (0.5, 9.0)], 0
        rows = build_training_rows(self.refills, temps, min_period_days=2, max_days=120)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 4.0)
        self.assertEqual(rows[0][1], 80)
        self.assertEqual(rows[0][2], [(0.5, 7.0), (0.5, 9.0)])
        self.assertEqual(len(calls), 2)

    def test_training_rows_rejects_short_and_overlong_periods(self):
        rows = build_training_rows(
            [{"ts": "2026-01-01T00:00:00+00:00", "kg": 0},
             {"ts": "2026-01-02T00:00:00+00:00", "kg": 20},
             {"ts": "2026-08-01T00:00:00+00:00", "kg": 30}],
            lambda *_: ([(1.0, 5.0)], 0), min_period_days=2, max_days=120,
        )
        self.assertEqual(rows, [])

    def test_model_selects_minimum_sse_and_preserves_metadata(self):
        scores = {12.0: 4.0, 12.5: 2.0, 13.0: 3.0}
        def fit(_rows, base):
            return {"balance_temp": base, "r2": 0.9, "_sse": scores[base],
                    "base_kg_per_day": 2, "kg_per_hdd": 1}
        result = fit_house_model(
            [(3, 30, [(1, 5)])], min_model_periods=1,
            model_bases=[12.0, 12.5, 13.0], hdd_base_fixed=None, fit_at=fit,
        )
        self.assertEqual(result["balance_temp"], 12.5)
        self.assertFalse(result["base_fixed"])
        self.assertEqual(result["best_fit_balance_temp"], 12.5)
        self.assertEqual(result["best_fit_r2"], 0.9)
        self.assertNotIn("_sse", result)

    def test_fixed_base_is_used_but_best_fit_metadata_is_retained(self):
        def fit(_rows, base):
            return {"balance_temp": base, "r2": 0.8, "_sse": abs(base - 13),
                    "base_kg_per_day": 1, "kg_per_hdd": 2}
        result = fit_house_model(
            [(3, 30, [(1, 5)])], min_model_periods=1,
            model_bases=[12.0, 13.0, 14.0], hdd_base_fixed=15.0, fit_at=fit,
        )
        self.assertEqual(result["balance_temp"], 15.0)
        self.assertTrue(result["base_fixed"])
        self.assertEqual(result["best_fit_balance_temp"], 13.0)

    def test_insufficient_periods_returns_none_without_fitting(self):
        result = fit_house_model(
            [], min_model_periods=4, model_bases=[12.0], hdd_base_fixed=None,
            fit_at=lambda *_: self.fail("must not fit without enough periods"),
        )
        self.assertIsNone(result)

    def test_active_base_precedence(self):
        model = {"balance_temp": 13.5}
        self.assertEqual(active_balance_temperature(model, fixed_base=16, fallback_base=14), 16)
        self.assertEqual(active_balance_temperature(model, fixed_base=None, fallback_base=14), 13.5)
        self.assertEqual(active_balance_temperature(None, fixed_base=None, fallback_base=14), 14)

    def test_integrated_fit_from_refills(self):
        result = fit_from_refills(
            self.refills, lambda *_: ([(1.0, 5.0)], 0),
            min_period_days=2, max_days=120, min_model_periods=2,
            model_bases=[12.0, 13.0], hdd_base_fixed=None,
            fit_at=lambda rows, base: {"balance_temp": base, "r2": 0.5,
                                      "_sse": abs(base - 13), "base_kg_per_day": 2,
                                      "kg_per_hdd": 1},
        )
        self.assertEqual(result["balance_temp"], 13.0)


if __name__ == "__main__":
    unittest.main()
