import unittest
from thermoflow_shadow_evaluation_v2 import evaluate_shadow_v2, SCHEMA

NOW = 1_810_000_000


def ledger():
    cases = []
    for i, actual in enumerate((10.0, 12.0, 11.0, 14.0)):
        decision = NOW + i * 100
        outcome = decision + 50
        cases.append({"id": f"case-{i}", "decision_at": decision, "outcome_end": outcome,
            "label": {"verified": True, "available_at": outcome + 1, "value": actual},
            "baseline": {"issued_at": decision, "data_cutoff": decision - 1, "value": actual + 2},
            "candidate": {"issued_at": decision, "data_cutoff": decision - 1,
                          "value": actual + (0.5 if i < 3 else 3.0)}})
    return {"schema": "thermoflow-shadow-ledger/1", "task": "fuel-kg-per-day",
            "kind": "regression", "start": NOW, "end": NOW + 500,
            "as_of": NOW + 1000, "window_seconds": 100, "cases": cases}


class ShadowEvaluationV2Tests(unittest.TestCase):
    def test_global_metrics_coverage_and_stability(self):
        result = evaluate_shadow_v2(ledger())
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["coverage"]["paired_cases"], 4)
        self.assertAlmostEqual(result["paired_metrics"]["candidate"]["mae"], 1.125)
        self.assertAlmostEqual(result["paired_metrics"]["candidate"]["bias"], 1.125)
        self.assertEqual(result["promotion"], "disabled")
        self.assertGreater(result["chronological_stability"][1]["mae_change"], 0)

    def test_unverified_label_reduces_coverage(self):
        data = ledger()
        data["cases"][0]["label"]["verified"] = False
        result = evaluate_shadow_v2(data)
        self.assertEqual(result["coverage"]["verified_labels"], 3)
        self.assertEqual(result["coverage"]["paired_cases"], 3)
        self.assertAlmostEqual(result["paired_metrics"]["candidate"]["mae"], 1.0)

    def test_alert_missing_denominators_remain_null(self):
        data = ledger()
        data["kind"] = "alert"
        for case in data["cases"]:
            case["label"]["value"] = False
            case["baseline"]["value"] = False
            case["candidate"]["value"] = True
        result = evaluate_shadow_v2(data)
        self.assertIsNone(result["paired_metrics"]["candidate"]["false_negative_rate"])
        self.assertEqual(result["paired_metrics"]["candidate"]["false_positive_rate"], 1.0)

    def test_invalid_prediction_timeline_is_excluded(self):
        data = ledger()
        data["cases"][0]["candidate"]["issued_at"] = data["cases"][0]["decision_at"] + 1
        result = evaluate_shadow_v2(data)
        self.assertEqual(result["coverage"]["paired_cases"], 3)


if __name__ == "__main__":
    unittest.main()
