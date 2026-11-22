import unittest
from unittest.mock import patch

from thermoflow_data_readiness import build_readiness_report


class DataReadinessTests(unittest.TestCase):
    def test_requires_at_least_one_real_snapshot(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            build_readiness_report()

    def test_report_is_aggregate_only_and_keeps_offline_limits_explicit(self):
        burner = {
            "schema": "thermoflow-raw-fire/1", "sampling": "raw",
            "points": {"thermoflow-fire": [[1_800_000_000, 0], [1_800_000_060, 1]]},
        }
        observed = {
            "counts": {"complete": 1, "incomplete": 0, "segments": 1},
            "dataset": {"eligible_samples": 0, "rejected_samples": {"incomplete": 1}},
            "input_quality": {},
        }
        evaluated = {"status": "collecting", "eligible_samples": 0, "reason": "insufficient_samples"}
        with (patch("thermoflow_data_readiness.build_observed_cycle_dataset", return_value=observed),
              patch("thermoflow_data_readiness.evaluate_burner_model", return_value=evaluated)):
            report = build_readiness_report(burner=burner)
        self.assertEqual(report["schema"], "thermoflow-data-readiness/1")
        self.assertEqual(report["status"], "collecting")
        self.assertFalse(report["live_predictions_enabled"])
        self.assertEqual(report["modules"]["burner_ml"]["eligible_samples"], 0)
        self.assertEqual(report["modules"]["burner_ml"]["required_samples"], 25)
        self.assertNotIn("points", report)
        self.assertNotIn("model", report)
        self.assertEqual(report["promotion"], "disabled")

    def test_all_requested_benchmarks_must_evaluate_before_ready(self):
        burner = {
            "schema": "thermoflow-raw-fire/1", "sampling": "raw",
            "points": {"thermoflow-fire": []},
        }
        observed = {
            "counts": {"complete": 30, "incomplete": 0, "segments": 1},
            "dataset": {"eligible_samples": 25, "rejected_samples": {}},
            "input_quality": {},
        }
        evaluated = {"status": "evaluated", "eligible_samples": 25, "reason": None}
        with (patch("thermoflow_data_readiness.build_observed_cycle_dataset", return_value=observed),
              patch("thermoflow_data_readiness.evaluate_burner_model", return_value=evaluated)):
            report = build_readiness_report(burner=burner)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["modules"]["burner_ml"]["holdout_samples"], 5)


if __name__ == "__main__":
    unittest.main()
