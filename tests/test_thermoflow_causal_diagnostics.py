import unittest

from thermoflow_causal_diagnostics import build_hydraulic_hypotheses


class ThermoFlowCausalDiagnosticsTests(unittest.TestCase):
    def test_repeated_return_drops_are_reported_as_association_only(self):
        events = [
            {"event": "start", "association_status": "paired", "return_delta_c": -1.5},
            {"event": "start", "association_status": "paired", "return_delta_c": -2.0},
            {"event": "start", "association_status": "paired", "return_delta_c": -1.0},
        ]
        result = build_hydraulic_hypotheses({
            "pump_correlations": {"thermoflow-pump-floor": {"events": events}}
        })
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["causality"], "not_established")
        self.assertIn("korelacja", result["findings"][0]["interpretation"])

    def test_unpaired_and_stop_events_do_not_count(self):
        events = [
            {"event": "start", "association_status": "paired", "return_delta_c": -3},
            {"event": "start", "association_status": "insufficient_unique_contiguous_return_samples", "return_delta_c": -5},
            {"event": "stop", "association_status": "paired", "return_delta_c": -4},
        ]
        result = build_hydraulic_hypotheses({
            "pump_correlations": {"pump": {"events": events}}
        })
        self.assertEqual(result["evaluated_pumps"]["pump"]["paired_start_events"], 1)
        self.assertEqual(result["findings"], [])

    def test_three_events_without_consistent_drop_do_not_raise_finding(self):
        events = [
            {"event": "start", "association_status": "paired", "return_delta_c": -0.2},
            {"event": "start", "association_status": "paired", "return_delta_c": 0.5},
            {"event": "start", "association_status": "paired", "return_delta_c": -0.4},
        ]
        result = build_hydraulic_hypotheses({"pump_correlations": {"pump": {"events": events}}})
        self.assertEqual(result["evaluated_pumps"]["pump"]["status"], "no_repeated_drop_detected")
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
