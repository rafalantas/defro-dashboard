"""Tests for the explicit offline Burner registration workflow."""
import unittest
from unittest.mock import patch

from thermoflow_burner_registration import register_burner_report


class BurnerRegistrationWorkflowTests(unittest.TestCase):
    def test_non_evaluated_benchmark_skips_without_artifact_or_database_writes(self):
        with patch("thermoflow_burner_registration.evaluate_burner_model",
                   return_value={"status": "collecting", "reason": "insufficient_samples"}), \
             patch("thermoflow_burner_registration.build_live_artifact") as build, \
             patch("thermoflow_burner_registration.persist_burner_shadow_result") as persist:
            result = register_burner_report(object(), {"dataset": {}}, recorded_at=100)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "insufficient_samples")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["promotion"], "disabled")
        build.assert_not_called()
        persist.assert_not_called()

    def test_evaluated_benchmark_builds_artifact_and_uses_existing_strict_importer(self):
        report = {"schema": "thermoflow-observed-cycles/1"}
        benchmark = {
            "status": "evaluated",
            "evaluation": {
                "candidate": {"beats_both_baselines_on_mae": True},
                "baselines": {"previous_cycle": {"mae_minutes": 10.0}},
            },
        }
        artifact = {"schema": "thermoflow-burner-live-model/1"}
        stored = {"status": "persisted", "model_version": "1.0.0+abc", "inserted": {}}
        with patch("thermoflow_burner_registration.evaluate_burner_model",
                   return_value=benchmark) as evaluate, \
             patch("thermoflow_burner_registration.build_live_artifact",
                   return_value=artifact) as build, \
             patch("thermoflow_burner_registration.persist_burner_shadow_result",
                   return_value=stored) as persist:
            result = register_burner_report("store", report, recorded_at=123)

        evaluate.assert_called_once_with(report)
        build.assert_called_once_with(benchmark, created_at=123)
        persist.assert_called_once_with("store", report, benchmark, artifact, recorded_at=123)
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(result["promotion"], "manual_review_required")
        self.assertEqual(result["registration"], stored)


if __name__ == "__main__":
    unittest.main()
