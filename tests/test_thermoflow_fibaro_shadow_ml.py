"""Tests for Fibaro daily shadow ML and its quality gate."""
import unittest
from datetime import date, timedelta

from thermoflow_fibaro_data_quality import SCHEMA as QUALITY_SCHEMA
from thermoflow_fibaro_shadow_ml import (
    FEATURE_KEYS, prepare_fibaro_daily_dataset, train_fibaro_daily_model,
)


def ready_quality():
    return {"schema": QUALITY_SCHEMA, "status": "ready",
            "training_gate": {"eligible": True, "promotion_enabled": False}}


def make_days(count=34):
    rows = []
    start = date(2026, 1, 1)
    for index in range(count):
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "heat_demand_started": float((index * 7) % 13 + 1),
            "window_opened": float((index * 5 + 2) % 7),
            "temperature_mean_absolute_delta_c": float((index * 3 + index // 4) % 11) / 10 + 0.1,
        })
    return rows


class FibaroShadowMLTests(unittest.TestCase):
    def test_quality_gate_blocks_training_before_dataset_preparation(self):
        quality = {"schema": QUALITY_SCHEMA, "status": "collecting",
                   "training_gate": {"eligible": False}}
        result = train_fibaro_daily_model(make_days(), quality)
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["model"])
        self.assertEqual(result["promotion"], "disabled")

    def test_consecutive_days_create_chronological_next_day_samples(self):
        dataset = prepare_fibaro_daily_dataset(make_days(), ready_quality())
        self.assertEqual(dataset["status"], "ready_for_shadow_training")
        self.assertEqual(dataset["feature_keys"], list(FEATURE_KEYS))
        self.assertEqual(dataset["eligible_samples"], 33)
        self.assertEqual(dataset["chronology"], "oldest_training_newest_holdout")
        self.assertLess(dataset["training"][-1]["timestamp"], dataset["holdout"][0]["timestamp"])

    def test_missing_day_is_not_synthesized_or_bridged(self):
        days = make_days()
        days.pop(10)
        dataset = prepare_fibaro_daily_dataset(days, ready_quality())
        self.assertEqual(dataset["eligible_samples"], 30)
        self.assertGreater(dataset["rejected_days_or_pairs"]["non_consecutive_day"], 0)

    def test_missing_feature_excludes_pair_without_imputation(self):
        days = make_days()
        days[4]["window_opened"] = None
        dataset = prepare_fibaro_daily_dataset(days, ready_quality())
        self.assertEqual(dataset["eligible_samples"], 32)
        self.assertGreater(dataset["rejected_days_or_pairs"]["missing_feature"], 0)

    def test_training_returns_shadow_evaluation_and_no_promotion(self):
        result = train_fibaro_daily_model(make_days(), ready_quality())
        self.assertIn(result["status"], ("evaluated", "unavailable"))
        self.assertEqual(result["mode"], "offline_shadow_only")
        self.assertEqual(result["promotion"], "disabled")
        if result["status"] == "evaluated":
            self.assertIsNotNone(result["evaluation"])
            self.assertTrue(result["evaluation"]["holdout_is_unseen"])

    def test_invalid_quality_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_fibaro_daily_dataset(make_days(), {"schema": "wrong"})


if __name__ == "__main__":
    unittest.main()
