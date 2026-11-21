import unittest

from test_thermoflow_anomaly_v2 import points
from thermoflow_anomaly_v2 import screen_series
from thermoflow_diagnostic_ai import diagnostic_hypotheses
from thermoflow_health_score import build_health_score


class DiagnosticAiTests(unittest.TestCase):
    def test_deviation_references_matching_evidence(self):
        reports = {"return": screen_series(points(), as_of=1720, minimum_deviation=2)}
        health = build_health_score(reports, expected=("return", "flow"), as_of=1720)
        result = diagnostic_hypotheses(health, reports)
        self.assertEqual(result["status"], "review")
        self.assertEqual(result["items"][0]["status"], "unconfirmed")
        self.assertEqual(result["items"][0]["evidence_ref"], "/screens/return")
        self.assertEqual(result["coverage"]["usable"], 1)

    def test_missing_or_mismatched_evidence_produces_no_hypothesis(self):
        reports = {"return": screen_series(points(), as_of=1720, minimum_deviation=2)}
        health = build_health_score(reports, expected=("return", "flow"), as_of=1720)
        reports["return"]["latest_timestamp"] -= 1
        self.assertEqual(diagnostic_hypotheses(health, reports)["items"], [])
        empty = build_health_score({}, expected=("return",), as_of=1720)
        self.assertEqual(diagnostic_hypotheses(empty, {})["status"], "unknown")
