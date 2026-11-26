"""Regression tests for the Fibaro-specific seasonal rolling validator."""
import unittest
from datetime import date, timedelta
from math import sin, cos

from thermoflow_fibaro_data_quality import SCHEMA as QUALITY_SCHEMA
from thermoflow_fibaro_rolling_validation import validate_fibaro_daily_rolling


class FibaroRollingValidationTests(unittest.TestCase):
    def daily_rows(self, count=110):
        first = date(2026, 1, 1)
        rows = []
        for index in range(count):
            heat = sin(index * 0.4) + index * 0.001
            windows = cos(index * 0.3) + index * 0.002
            temp = ((index * 7) % 13) + index * 0.003
            rows.append({
                "date": (first + timedelta(days=index)).isoformat(),
                "heat_demand_started": 10 + heat,
                "window_opened": 2 + windows,
                "temperature_mean_absolute_delta_c": 1 + temp,
            })
        return rows

    def quality(self):
        return {"schema": QUALITY_SCHEMA, "status": "ready",
                "training_gate": {"eligible": True}}

    def test_validates_actual_fibaro_daily_target_over_seasonal_folds(self):
        result = validate_fibaro_daily_rolling(self.daily_rows(), self.quality())
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["task"], "next_day_heat_required_starts")
        self.assertEqual(result["fold_count"], 5)
        self.assertEqual(result["holdout_samples_total"], 25)
        self.assertEqual(result["promotion"], "disabled")
        self.assertTrue(result["coverage"]["span_satisfied"])
        self.assertTrue(result["coverage"]["months_satisfied"])

    def test_insufficient_fibaro_history_stays_collecting(self):
        result = validate_fibaro_daily_rolling(self.daily_rows(50), self.quality())
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["promotion"], "disabled")

    def test_quality_gate_blocks_rolling_validation(self):
        quality = {"schema": QUALITY_SCHEMA, "status": "blocked",
                   "training_gate": {"eligible": False}}
        result = validate_fibaro_daily_rolling(self.daily_rows(), quality)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "fibaro_quality_gate_not_ready")


if __name__ == "__main__":
    unittest.main()
