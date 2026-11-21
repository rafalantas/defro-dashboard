import unittest
from datetime import datetime, timedelta, timezone

from fuel_hydraulic_correlation import build_hydraulic_correlation


class HydraulicCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)

    def test_pump_transition_correlates_observed_return_and_valve_windows(self):
        points = {
            "pump_dhw": [
                (self.start, 0), (self.start + timedelta(minutes=1), 1),
            ],
            "return_c": [
                (self.start - timedelta(minutes=4), 56),
                (self.start - timedelta(minutes=2), 55),
                (self.start + timedelta(minutes=1), 53),
                (self.start + timedelta(minutes=3), 52),
            ],
            "valve_opening_pct": [
                (self.start - timedelta(minutes=3), 20),
                (self.start - timedelta(minutes=1), 22),
                (self.start + timedelta(minutes=1), 30),
                (self.start + timedelta(minutes=3), 35),
            ],
        }
        result = build_hydraulic_correlation(points)
        self.assertEqual(result["pump_transitions_count"], 1)
        event = result["pump_transitions"][0]
        self.assertEqual(event["transition"], "on")
        response = event["return_response"]
        self.assertEqual(response["response_status"], "observed_both_windows")
        self.assertEqual(response["return_mean_delta_c"], -3.0)
        self.assertEqual(response["valve_mean_delta_pct"], 11.5)

    def test_valve_movement_requires_five_percentage_points(self):
        points = {"valve_opening_pct": [
            (self.start, 20),
            (self.start + timedelta(minutes=1), 23),
            (self.start + timedelta(minutes=2), 29),
        ]}
        result = build_hydraulic_correlation(points)
        self.assertEqual(result["valve_movements_count"], 1)
        self.assertEqual(result["valve_movements"][0]["change_pct"], 6.0)

    def test_gap_splits_transition_and_does_not_interpolate(self):
        points = {
            "pump_floor": [
                (self.start, 0),
                (self.start + timedelta(minutes=8), 1),
            ],
            "return_c": [
                (self.start - timedelta(minutes=1), 55),
                (self.start + timedelta(minutes=9), 52),
            ],
        }
        result = build_hydraulic_correlation(points)
        self.assertEqual(result["pump_transitions_count"], 0)

    def test_insufficient_coverage_is_explicit(self):
        points = {
            "pump_dhw": [(self.start, 0), (self.start + timedelta(minutes=1), 1)],
            "return_c": [(self.start, 54), (self.start + timedelta(minutes=1), 53)],
        }
        result = build_hydraulic_correlation(points)
        response = result["pump_transitions"][0]["return_response"]
        self.assertEqual(response["response_status"], "insufficient_window_coverage")
        self.assertIsNone(response["return_mean_delta_c"])

    def test_empty_input_is_safe(self):
        result = build_hydraulic_correlation({})
        self.assertEqual(result["pump_transitions_count"], 0)
        self.assertEqual(result["valve_movements_count"], 0)


if __name__ == "__main__":
    unittest.main()
