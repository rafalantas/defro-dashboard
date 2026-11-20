import unittest
from thermoflow_diagnostic_intelligence import build_thermoflow_intelligence


class ThermoFlowDiagnosticIntelligenceTests(unittest.TestCase):
    def test_empty_history_is_safe_and_ml_waits_for_data(self):
        result = build_thermoflow_intelligence({}, {})
        self.assertEqual(result["diagnostic_core"]["status"], "collecting")
        self.assertEqual(result["ml_baseline"]["status"], "collecting")
        self.assertEqual(result["burner_ml_baseline"]["status"], "collecting")
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

    def test_fire_diagnostics_validate_binary_state_and_keep_brightness_separate(self):
        start = 1795161600
        raw = {
            "thermoflow-fire": [[start, 0], [start + 60, 1], [start + 120, 1], [start + 180, 2]],
            "thermoflow-fire-brightness": [[start, 0], [start + 60, 42], [start + 120, 51]],
        }
        result = build_thermoflow_intelligence(raw)
        fire = result["fire_diagnostics"]
        self.assertEqual(fire["flame_state"]["valid_binary_samples"], 3)
        self.assertEqual(fire["flame_state"]["invalid_samples"], 1)
        self.assertEqual(fire["brightness"]["max"], 51)
        self.assertEqual(fire["quality"]["status"], "review")

    def test_burner_cycles_use_debounced_fire_and_cross_check_other_series(self):
        start = 1795161600
        raw = {
            "thermoflow-fire": [[start, 0], [start + 60, 1], [start + 120, 1],
                                [start + 600, 0], [start + 660, 0]],
            "thermoflow-flue": [[start + 120, 80]],
            "thermoflow-boiler-power": [[start + 120, 15]],
        }
        result = build_thermoflow_intelligence(raw)
        burner = result["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 1)
        self.assertEqual(burner["cycles"][0]["status"], "confirmed_by_fire_state")
        self.assertEqual(burner["cycles"][0]["duration_minutes"], 9.0)
        self.assertEqual(result["cycle_analyzer"]["source"],
                         "existing_timestamped_4d_protection_cycle_analysis")

    def test_hydraulic_diagnostics_report_pump_starts_and_stops(self):
        start = 1795161600
        raw = {
            "thermoflow-valve4d-return": [[start, 56], [start + 60, 54], [start + 120, 55], [start + 180, 56]],
            "thermoflow-pump-floor": [[start, 0], [start + 60, 1], [start + 120, 0]],
        }
        result = build_thermoflow_intelligence(raw)
        pump = result["hydraulic_diagnostics"]["pump_correlations"]["thermoflow-pump-floor"]
        self.assertEqual(pump["starts"], 1)
        self.assertEqual(pump["stops"], 1)
        self.assertEqual(pump["paired_events"], 2)

    def test_burner_baseline_uses_chronological_burner_cycles(self):
        start = 1795161600
        fire = []
        for index in range(25):
            base = start + index * 3600
            fire.extend([[base, 0], [base + 60, 1], [base + 120, 1],
                         [base + 660, 0], [base + 720, 0]])
        result = build_thermoflow_intelligence({"thermoflow-fire": fire})
        model = result["burner_ml_baseline"]
        self.assertEqual(model["status"], "evaluated")
        self.assertEqual(model["training_cycles"], 20)
        self.assertEqual(model["holdout_cycles"], 5)
        self.assertEqual(model["validation"], "chronological_last_5_burner_cycles_holdout")


    def test_burner_cycles_never_join_across_telemetry_gap(self):
        start = 1795161600
        raw = {"thermoflow-fire": [
            [start, 0], [start + 60, 1], [start + 120, 1],
            [start + 900, 0], [start + 960, 0],
        ]}
        burner = build_thermoflow_intelligence(raw)["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 0)
        self.assertEqual(burner["incomplete"], 1)
        self.assertEqual(burner["incomplete_cycles"][0]["reason"], "telemetry_gap")
        self.assertEqual(burner["timeline_segments"], 2)

    def test_burner_cycles_ignore_single_sample_spike(self):
        start = 1795161600
        raw = {"thermoflow-fire": [
            [start, 0], [start + 60, 1], [start + 120, 0],
            [start + 180, 1], [start + 240, 1],
            [start + 600, 0], [start + 660, 0],
        ]}
        burner = build_thermoflow_intelligence(raw)["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 1)
        self.assertEqual(burner["ignored_transitions"], 2)
        self.assertEqual(burner["cycles"][0]["duration_minutes"], 7.0)

    def test_burner_cycle_starting_on_is_left_censored_not_baseline_eligible(self):
        start = 1795161600
        raw = {"thermoflow-fire": [
            [start, 1], [start + 60, 1], [start + 300, 0], [start + 360, 0],
        ]}
        result = build_thermoflow_intelligence(raw)
        burner = result["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 0)
        self.assertEqual(burner["incomplete_cycles"][0]["reason"],
                         "left_censored_initial_on_state")
        self.assertEqual(result["burner_ml_baseline"]["status"], "collecting")

    def test_burner_cycle_open_at_end_is_right_censored(self):
        start = 1795161600
        raw = {"thermoflow-fire": [
            [start, 0], [start + 60, 1], [start + 120, 1],
        ]}
        burner = build_thermoflow_intelligence(raw)["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 0)
        self.assertEqual(burner["incomplete_cycles"][0]["reason"],
                         "right_censored_open_cycle")

    def test_burner_cycle_boundary_gap_equal_to_limit_is_not_split(self):
        start = 1795161600
        raw = {"thermoflow-fire": [
            [start, 0], [start + 60, 1], [start + 120, 1],
            [start + 420, 0], [start + 480, 0],
        ]}
        burner = build_thermoflow_intelligence(raw)["burner_cycle_analyzer"]
        self.assertEqual(burner["complete"], 1)
        self.assertEqual(burner["timeline_segments"], 1)

    def test_ai_exposes_evidence_and_is_explicitly_rule_based(self):
        result = build_thermoflow_intelligence({"thermoflow-fire": [[1795161600, 1]]})
        self.assertIn("evidence", result["thermoflow_ai"])
        self.assertIn("nie LLM", result["thermoflow_ai"]["note"])


if __name__ == "__main__":
    unittest.main()

