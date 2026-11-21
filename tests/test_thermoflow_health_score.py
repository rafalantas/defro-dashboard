import unittest

from test_thermoflow_anomaly_v2 import points
from thermoflow_anomaly_v2 import screen_series
from thermoflow_health_score import build_health_score


class HealthScoreTests(unittest.TestCase):
    def setUp(self):
        self.reports = {
            name: screen_series(points(value), as_of=1720, minimum_deviation=2)
            for name, value in (("flow", 20), ("return", 26))
        }

    def test_equal_weight_explained_score(self):
        result = build_health_score(self.reports, expected=("flow", "return"), as_of=1720)
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["components"]["return"]["reason"], "review_deviation")

    def test_missing_component_is_not_scored(self):
        result = build_health_score(self.reports, expected=("flow", "missing"), as_of=1720)
        self.assertIsNone(result["score"])
        self.assertEqual(result["coverage"], {"usable": 1, "expected": 2})

    def test_old_reports_cannot_pass_as_fresh(self):
        result = build_health_score(self.reports, expected=("flow",), as_of=2000)
        self.assertIsNone(result["score"])
        self.assertEqual(result["components"]["flow"]["age_seconds"], 280)
        self.assertEqual(result["components"]["flow"]["reason"], "stale_screen")
