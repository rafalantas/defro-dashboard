import unittest
from thermoflow_model_monitoring import monitor_models

NOW = 1_810_000_000


def ledger(candidate_offset=0.5, coverage=True):
    cases = []
    for i, actual in enumerate((10., 11., 12., 13.)):
        decision = NOW - 1000 + i * 100
        outcome = decision + 30
        cases.append({"id":str(i), "decision_at":decision, "outcome_end":outcome,
            "label":{"verified":coverage, "available_at":outcome+1, "value":actual},
            "baseline":{"issued_at":decision, "data_cutoff":decision-1, "value":actual+1},
            "candidate":{"issued_at":decision, "data_cutoff":decision-1, "value":actual+candidate_offset}})
    return {"schema":"thermoflow-shadow-ledger/1", "task":"fuel-kg-per-day",
        "kind":"regression", "start":NOW-1000, "end":NOW-500, "as_of":NOW,
        "window_seconds":125, "cases":cases}


class ModelMonitoringTests(unittest.TestCase):
    def test_missing_artifact_and_ledger_are_collecting_not_healthy(self):
        result = monitor_models(now=NOW)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "missing")
        self.assertIn("shadow_ledger_missing", result["modules"]["fuel_ml"]["alerts"])

    def test_stale_and_future_artifacts_are_rejected(self):
        result = monitor_models(now=NOW, artifacts={
            "burner_ml":{"created_at":NOW-40*86400},
            "fuel_ml":{"created_at":NOW+60}})
        self.assertEqual(result["modules"]["burner_ml"]["status"], "stale")
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "invalid")

    def test_error_regression_triggers_review_without_promotion(self):
        result = monitor_models(now=NOW,
            artifacts={"fuel_ml":{"created_at":NOW-60}},
            ledgers={"fuel_ml":ledger(candidate_offset=3.0)})
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "review")
        self.assertIn("candidate_error_regression", result["modules"]["fuel_ml"]["alerts"])
        self.assertEqual(result["promotion"], "disabled")

    def test_low_coverage_is_explicit(self):
        result = monitor_models(now=NOW,
            artifacts={"fuel_ml":{"created_at":NOW-60}},
            ledgers={"fuel_ml":ledger(coverage=False)})
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "collecting")

    def test_monitor_does_not_call_missing_data_good(self):
        result = monitor_models(now=NOW, artifacts={"burner_ml":{"created_at":NOW-5}})
        self.assertIn("shadow_ledger_missing", result["modules"]["burner_ml"]["alerts"])
        self.assertEqual(result["modules"]["burner_ml"]["status"], "collecting")


if __name__ == "__main__":
    unittest.main()
