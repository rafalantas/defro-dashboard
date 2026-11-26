"""Tests for season-aware expanding-window ThermoFlow ML validation."""
import unittest
from datetime import datetime, timedelta, timezone

from thermoflow_ml_validation import validate_rolling_origin


class RollingValidationTests(unittest.TestCase):
    def samples(self, count=100, start=datetime(2026, 1, 1, tzinfo=timezone.utc)):
        rows = []
        for index in range(count):
            rows.append({
                "timestamp": (start + timedelta(days=index)).timestamp(),
                "complete": True,
                "outside_temp": float(index % 17) + index / 100,
                "target": 12.0 + 1.7 * (float(index % 17) + index / 100),
            })
        return rows

    def validate(self, samples):
        return validate_rolling_origin(
            samples, feature_keys=("outside_temp",), target_key="target"
        )

    def test_runs_expanding_chronological_folds_with_unseen_holdouts(self):
        result = self.validate(self.samples())
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["fold_count"], 3)
        self.assertEqual([fold["training_samples"] for fold in result["folds"]], [40, 45, 50])
        self.assertEqual(result["holdout_samples_total"], 15)
        for fold in result["folds"]:
            self.assertLess(fold["training_end_timestamp"], fold["holdout_start_timestamp"])
        self.assertTrue(result["coverage"]["span_satisfied"])
        self.assertTrue(result["coverage"]["months_satisfied"])

    def test_requires_minimum_number_of_eligible_samples(self):
        result = self.validate(self.samples(54))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "insufficient_eligible_samples")

    def test_requires_multiple_months_and_minimum_time_span(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = self.validate(self.samples(55, start))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "insufficient_seasonal_coverage")
        self.assertFalse(result["coverage"]["span_satisfied"])

    def test_rejects_weak_validation_configuration(self):
        with self.assertRaises(ValueError):
            validate_rolling_origin(
                self.samples(), feature_keys=("outside_temp",), target_key="target",
                minimum_training_samples=10,
            )

    def test_input_rows_are_not_mutated(self):
        rows = self.samples()
        before = [dict(row) for row in rows]
        self.validate(rows)
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
