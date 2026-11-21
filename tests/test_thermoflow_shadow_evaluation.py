import unittest

from thermoflow_shadow_evaluation import INPUT_SCHEMA, evaluate_shadow


def case(identifier, decision, actual, baseline, candidate):
    return {"id": identifier, "decision_at": decision, "outcome_end": decision + 6,
            "label": {"verified": True, "available_at": decision + 8, "value": actual},
            "baseline": {"issued_at": decision - 1, "data_cutoff": decision - 2,
                         "value": baseline, "duration_ms": 2},
            "candidate": {"issued_at": decision, "data_cutoff": decision,
                          "value": candidate, "duration_ms": 5}}


def ledger(kind, cases):
    return {"schema": INPUT_SCHEMA, "task": "example", "kind": kind, "start": 100, "end": 400,
            "as_of": 400, "window_seconds": 100, "cases": cases}


class ShadowEvaluationTests(unittest.TestCase):
    def test_windows_include_empty_and_compare_paired_regression(self):
        rows = [case("b", 200, 10, 14, 9), case("a", 190, 10, 13, 11)]
        result = evaluate_shadow(ledger("regression", rows))
        self.assertEqual(result["promotion"], "disabled")
        left, right, empty = result["windows"]
        self.assertEqual([w["paired_labeled"] for w in (left, right, empty)], [1, 1, 0])
        self.assertEqual(right["paired_metrics"]["baseline"]["mae"], 4)
        self.assertEqual(right["paired_metrics"]["candidate"]["bias"], -1)
        self.assertEqual(right["recorded_compute"]["candidate"]["p95_ms"], 5)
        self.assertIsNone(empty["paired_metrics"]["baseline"])

    def test_late_prediction_and_label_do_not_score(self):
        bad_prediction = case("early", 160, 10, 14, 9)
        bad_prediction["candidate"]["issued_at"] = 161
        unknown_label = case("late", 395, 11, 12, 10)
        result = evaluate_shadow(ledger("regression", [bad_prediction, unknown_label]))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["windows"][0]["coverage"]["candidate"]["invalid"], 1)
        self.assertEqual(result["windows"][0]["verified_labels"], 1)
        self.assertEqual(result["windows"][2]["verified_labels"], 0)
        self.assertEqual(result["windows"][2]["coverage"]["candidate"]["valid"], 1)

    def test_alerts_use_labeled_negatives_and_exclude_duplicates(self):
        repeat = case("duplicate", 105, False, True, False)
        rows = [repeat, repeat, case("negative", 120, False, True, False),
                case("positive", 125, True, True, False), case("only-positive", 220, True, True, True)]
        result = evaluate_shadow(ledger("alert", rows))
        self.assertEqual(result["excluded_cases"], {"duplicate_case": 2})
        first, second, _ = result["windows"]
        self.assertEqual(first["paired_metrics"]["baseline"]["false_positive_rate"], 1)
        self.assertEqual(first["paired_metrics"]["candidate"]["false_negative_rate"], 1)
        self.assertIsNone(second["paired_metrics"]["candidate"]["false_positive_rate"])
