"""Regression tests for the extracted daily-model selection."""
import unittest

from fuel_daily_model import fit_daily_model


class DailyModelExtractionTests(unittest.TestCase):
    def setUp(self):
        self.cache = {"key": None, "data": None}
        self.base_calls = []

    def active_base(self):
        self.base_calls.append(True)
        return 14.0

    def rows(self, count):
        return [{
            "day": f"2026-10-{(i % 28) + 1:02d}",
            "kg_counter": 20.0 + i,
            "hdd": 8.0 + i / 10,
            "hdd_lag": 7.5 + i / 10,
            "solar_mj": 4.0,
            "wind_max_kmh": 10.0,
            "estimate": False,
        } for i in range(count)]

    def fit(self, x, y):
        # Deterministic stand-in: positive HDD slope, neutral model comparison.
        return {"beta": [1.0] + [1.0] * (len(x[0]) - 1),
                "rmse": 2.0, "rmse_loo": 2.0}

    def call(self, rows):
        return fit_daily_model(
            rows, cache=self.cache, active_base=self.active_base,
            min_days=30, lag_weight_today=0.6, lag_weight_previous=0.4,
            min_gain=0.03, ols_loo=self.fit,
        )

    def test_insufficient_history_keeps_legacy_status_and_caches(self):
        result = self.call(self.rows(29))
        self.assertEqual(result, {"status": "zbiera dane", "days": 29, "days_needed": 30})
        self.assertEqual(self.cache["data"], result)

    def test_eligible_history_builds_model_and_reuses_cache(self):
        rows = self.rows(30)
        first = self.call(rows)
        self.assertEqual(first["status"], "aktywny")
        self.assertEqual(first["days"], 30)
        self.assertEqual(first["balance_temp"], 14.0)
        self.assertEqual(first["temperature_term"], "t_mean")
        self.assertEqual(first["kg_per_hdd"], 1.0)
        calls = len(self.base_calls)
        second = self.call(rows)
        self.assertIs(second, first)
        self.assertEqual(len(self.base_calls), calls + 1)

    def test_temporary_counter_days_are_excluded(self):
        rows = self.rows(30)
        for row in rows[:5]:
            row["estimate"] = True
        result = self.call(rows)
        self.assertEqual(result["status"], "zbiera dane")
        self.assertEqual(result["days"], 25)


if __name__ == "__main__":
    unittest.main()
