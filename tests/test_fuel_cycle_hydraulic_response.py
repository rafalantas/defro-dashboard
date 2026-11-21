import unittest
from datetime import datetime, timedelta, timezone

from fuel_cycle_hydraulic_response import build_cycle_hydraulic_response


class CycleHydraulicResponseTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)

    def test_correlates_cycle_with_pump_valve_and_return_response(self):
        normalized = {"return_c": [
            (self.start - timedelta(minutes=4), 56),
            (self.start - timedelta(minutes=2), 55),
            (self.start + timedelta(minutes=2), 53),
            (self.start + timedelta(minutes=4), 52),
        ]}
        hydraulic = {
            "pump_transitions": [{"at": (self.start + timedelta(minutes=3)).isoformat()}],
            "valve_movements": [{"at": (self.start + timedelta(minutes=5)).isoformat()}],
        }
        cycles = [{"at": self.start.isoformat(), "flame_presence_observed": True,
                   "temperature_recovery_observed": False}]
        result = build_cycle_hydraulic_response(normalized, cycles, hydraulic)
        cycle = result["cycles"][0]
        self.assertEqual(result["cycles_with_return_coverage"], 1)
        self.assertEqual(cycle["return_window"]["mean_delta_c"], -3.0)
        self.assertEqual(cycle["pump_transitions_within_15m"], 1)
        self.assertEqual(cycle["valve_movements_within_15m"], 1)
        self.assertEqual(cycle["interpretation"], "temporal_cooccurrence_not_causality")

    def test_gap_or_insufficient_samples_suppresses_delta(self):
        normalized = {"return_c": [
            (self.start - timedelta(minutes=4), 56),
            (self.start + timedelta(minutes=8), 52),
            (self.start + timedelta(minutes=9), 51),
        ]}
        result = build_cycle_hydraulic_response(
            normalized, [{"at": self.start.isoformat()}], {})
        response = result["cycles"][0]["return_window"]
        self.assertEqual(response["coverage_status"], "insufficient_or_gapped")
        self.assertIsNone(response["mean_delta_c"])

    def test_invalid_cycle_timestamp_is_skipped(self):
        result = build_cycle_hydraulic_response({}, [{"at": "bad"}], {})
        self.assertEqual(result["cycles_analyzed"], 0)

    def test_empty_input_is_safe(self):
        result = build_cycle_hydraulic_response({}, [], {})
        self.assertEqual(result["cycles"], [])
        self.assertEqual(result["cycles_with_return_coverage"], 0)


if __name__ == "__main__":
    unittest.main()
