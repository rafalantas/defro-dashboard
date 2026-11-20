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
        self.assertEqual(c["fire_sensor"]["status"], "not_available_in_history_window")
        self.assertIn("Brak próbek FIRE", c["fire_sensor"]["note"])

    def test_ml_uses_last_five_cycles_as_chronological_holdout(self):
        cycles = [{"complete": True, "cold_gap_minutes": 10 + i % 4,
                   "protection_start": str(i)} for i in range(25)]
        result = build_thermoflow_intelligence({}, {"protection_cycles": cycles})
        self.assertEqual(result["ml_baseline"]["status"], "evaluated")
        self.assertEqual(result["ml_baseline"]["training_cycles"], 20)
        self.assertEqual(result["ml_baseline"]["holdout_cycles"], 5)
        self.assertEqual(result["ml_baseline"]["holdout_metrics"]["observations"], 5)
        self.assertGreaterEqual(result["ml_baseline"]["holdout_metrics"]["mae_minutes"], 0)
        self.assertIn("rmse_minutes", result["ml_baseline"]["holdout_metrics"])
        self.assertIn("bias_minutes", result["ml_baseline"]["holdout_metrics"])
        self.assertEqual(result["predictive_diagnostics"]["status"], "experimental")

    def test_core_reports_duplicate_malformed_and_out_of_order_samples(self):
        start = 1795161600
        raw = {"thermoflow-flue": [[start + 60, 80], [start, 75],
                                   [start, 76], [start + 120, "bad"]]}
        quality = build_thermoflow_intelligence(raw)["diagnostic_core"]["series"]["thermoflow-flue"]
        self.assertEqual(quality["duplicate_timestamps"], 1)
        self.assertEqual(quality["out_of_order_samples"], 1)
        self.assertEqual(quality["malformed_samples"], 1)
        self.assertEqual(quality["coverage_status"], "review")

    def test_fire_samples_are_joined_by_time_without_inventing_semantics(self):
        start = 1795161600
        raw = {"thermoflow-flue": [[start, 75], [start + 60, 82]],
               "thermoflow-fire": [[start + 30, 1]]}
        combustion = build_thermoflow_intelligence(raw)["combustion_diagnostics"]
        self.assertEqual(combustion["fire_join_status"], "available")
        self.assertEqual(combustion["fire_flue_time_correlations"][0]["fire_samples_within_5m"], 1)
        self.assertIn("semantics are not inferred",
                      combustion["fire_flue_time_correlations"][0]["interpretation"])
        self.assertEqual(combustion["fire_sensor"]["status"], "available_in_thermoflow_history")
        self.assertEqual(combustion["fire_sensor"]["fields"]["field1"], "flame_state")
        self.assertEqual(combustion["fire_sensor"]["fields"]["field2"], "brightness")

    def test_insufficient_cycles_do_not_train(self):
        cycles = [{"complete": True, "cold_gap_minutes": 10} for _ in range(24)]
        result = build_thermoflow_intelligence({}, {"protection_cycles": cycles})
        self.assertEqual(result["ml_baseline"]["status"], "collecting")
        self.assertEqual(result["ml_baseline"]["training_cycles"], 0)


if __name__ == "__main__":
    unittest.main()
