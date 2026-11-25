"""Tests for Fibaro shadow chronological backtesting and readiness."""
import unittest
from datetime import date, timedelta

from thermoflow_fibaro_shadow_evaluation import evaluate_fibaro_shadow


def model_result():
    first_train = date(2026, 9, 1)
    training = [
        {"timestamp": int((first_train + timedelta(days=index)).strftime("%Y%m%d")),
         "target": float(value),
         "features": {"previous_day_heat_starts": 1.0, "previous_day_window_opens": 1.0,
                      "previous_day_temperature_abs_delta_c": 1.0}}
        for index, value in enumerate([4, 5, 3, 4, 5, 3, 4, 5, 3, 4, 5, 3, 4, 5, 3, 4, 5, 3, 4, 5])
    ]
    first_holdout = date(2026, 10, 1)
    holdout = [
        {"timestamp": int((first_holdout + timedelta(days=index)).strftime("%Y%m%d")),
         "target": float(value),
         "features": {"previous_day_heat_starts": float(index),
                      "previous_day_window_opens": float(index % 2),
                      "previous_day_temperature_abs_delta_c": float(index) / 10}}
        for index, value in enumerate([2, 3, 4, 3, 2])
    ]
    model = {
        "model_schema_version": "1.0.0", "mode": "read_only_shadow",
        "feature_keys": ["previous_day_heat_starts", "previous_day_window_opens",
                         "previous_day_temperature_abs_delta_c"],
        "target_key": "next_day_heat_starts",
        "feature_means": [0.0, 0.0, 0.0], "feature_scales": [1.0, 1.0, 1.0],
        "intercept": 3.0, "coefficients": [0.0, 0.0, 0.0],
    }
    return {"status": "evaluated", "dataset": {"training": training, "holdout": holdout},
            "model": model, "evaluation": {"holdout_is_unseen": True}}


class FibaroShadowEvaluationTests(unittest.TestCase):
    def test_creates_daily_chronological_backtest_and_readiness(self):
        result = evaluate_fibaro_shadow(model_result())
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["backtest"]["mode"], "offline_shadow_only")
        self.assertEqual(result["backtest"]["promotion"], "disabled")
        self.assertEqual(result["summary"]["holdout_days"], 5)
        self.assertEqual(len(result["backtest"]["windows"]), 5)
        self.assertTrue(result["readiness"]["all_holdout_labels_verified"])
        self.assertFalse(result["readiness"]["automatic_promotion"])

    def test_candidate_must_beat_baseline_for_review_readiness(self):
        result = model_result()
        result["model"]["intercept"] = 100.0
        evaluated = evaluate_fibaro_shadow(result)
        self.assertEqual(evaluated["readiness"]["status"], "not_ready")
        self.assertFalse(evaluated["summary"]["beats_baseline"])

    def test_missing_model_returns_collecting(self):
        result = evaluate_fibaro_shadow({"status": "collecting"})
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["readiness"]["status"], "not_ready")

    def test_rejects_model_without_unseen_holdout_proof(self):
        result = model_result()
        result["evaluation"]["holdout_is_unseen"] = False
        with self.assertRaises(ValueError):
            evaluate_fibaro_shadow(result)

    def test_requires_minimum_holdout_days(self):
        result = model_result()
        result["dataset"]["holdout"] = result["dataset"]["holdout"][:4]
        evaluated = evaluate_fibaro_shadow(result)
        self.assertEqual(evaluated["status"], "collecting")
        self.assertEqual(evaluated["reason"], "minimum_five_holdout_days_required")


if __name__ == "__main__":
    unittest.main()
