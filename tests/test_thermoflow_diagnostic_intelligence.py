import unittest
from thermoflow_diagnostic_intelligence import build_thermoflow_intelligence


class ThermoFlowDiagnosticIntelligenceTests(unittest.TestCase):
    def test_empty_history_is_safe_and_ml_waits_for_data(self):
        result = build_thermoflow_intelligence({}, {})
        self.assertEqual(result["diagnostic_core"]["status"], "collecting")
        self.assertEqual(result["ml_baseline"]["status"], "collecting")
        self.assertEqual(result["predictive_diagnostics"]["status"], "unavailable")
        self.assertIn("no control", result["safety"])

    def test_low_return_is_warning_not_protection_failure(self):
        start = 1795161600
        raw = {"thermoflow-valve4d-return": [[start, 53.0], [start + 60, 52.5]],
               "thermoflow-pump-dhw": [[start, 0], [start + 60, 1]]}
        result = build_thermoflow_intelligence(raw)
        h = result["hydraulic_diagnostics"]
        self.assertEqual(h["return_below_54c_samples"], 2)
        self.assertEqual(h["reference_return_protection_c"], 55.0)
        self.assertIn("nie potwierdzają zadziałania ochrony", h["interpretation"])

    def test_flue_is_not_ignition_and_fire_stays_in_audit(self):
        result = build_thermoflow_intelligence({"thermoflow-flue": [[1795161600, 85]]})
        c = result["combustion_diagnostics"]
        self.assertFalse(c["elevated_flue_episodes"][0]["ignition_confirmed"])
        self.assertEqual(c["fire_sensor"]["status"], "separate_fuel_telemetry_audit")

    def test_ml_uses_last_five_cycles_as_chronological_holdout(self):
        cycles = [{"complete": True, "cold_gap_minutes": 10 + i % 4,
                   "protection_start": str(i)} for i in range(25)]
        result = build_thermoflow_intelligence({}, {"protection_cycles": cycles})
        self.assertEqual(result["ml_baseline"]["status"], "shadow_validated")
        self.assertEqual(result["ml_baseline"]["training_cycles"], 20)
        self.assertEqual(result["ml_baseline"]["holdout_cycles"], 5)
        self.assertEqual(result["predictive_diagnostics"]["status"], "experimental")

    def test_insufficient_cycles_do_not_train(self):
        cycles = [{"complete": True, "cold_gap_minutes": 10} for _ in range(24)]
        result = build_thermoflow_intelligence({}, {"protection_cycles": cycles})
        self.assertEqual(result["ml_baseline"]["status"], "collecting")
        self.assertEqual(result["ml_baseline"]["training_cycles"], 0)


if __name__ == "__main__":
    unittest.main()
