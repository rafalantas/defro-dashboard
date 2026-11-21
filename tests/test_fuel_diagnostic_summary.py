import unittest

from fuel_diagnostic_summary import build_diagnostic_summary


class DiagnosticSummaryTests(unittest.TestCase):
    def test_low_return_is_a_review_signal_not_a_protection_failure(self):
        result = build_diagnostic_summary(
            {"hydraulics": {
                "low_return_episodes": [
                    {"return_c": {"min": 51.5}},
                ],
                "warning_threshold_c": 54.0,
            }},
            {"series": {"return_c": {"samples": 20, "gaps_over_5m": 0}}},
        )
        finding = result["findings"][0]
        self.assertEqual(result["status"], "attention")
        self.assertEqual(finding["code"], "return_temperature_low_episode")
        self.assertEqual(finding["reference_protection_c"], 55.0)
        self.assertEqual(finding["minimum_return_c"], 51.5)
        self.assertIn("nie potwierdza awarii", finding["message"])

    def test_flue_activity_is_not_classified_as_confirmed_ignition(self):
        result = build_diagnostic_summary(
            {"firing_activity": {"episodes_count": 2}, "fire_activity": {"episodes_count": 0}},
            {"series": {"flue_c": {"samples": 50, "gaps_over_5m": 0}}},
        )
        finding = result["findings"][0]
        self.assertEqual(finding["code"], "flue_activity_without_flame_confirmation")
        self.assertEqual(finding["confidence"], "screening_only")
        self.assertIn("nie dowodem rozpalenia", finding["message"])

    def test_telemetry_gaps_and_raw_loss_are_reported_without_imputation(self):
        result = build_diagnostic_summary(
            {},
            {"series": {
                "return_c": {"samples": 10, "gaps_over_5m": 2},
                "co_c": {"samples": 0, "gaps_over_5m": 0},
            }},
            {"channels": [{
                "name": "temps",
                "fields": {"flue_c": {"loss_percent": 7.5, "lost_samples": 3}},
            }]},
        )
        codes = {item["code"] for item in result["findings"]}
        self.assertEqual(result["status"], "attention")
        self.assertIn("telemetry_coverage_gaps", codes)
        self.assertIn("raw_ingestion_loss", codes)

    def test_hydraulic_correlation_is_summarized_as_cooccurrence(self):
        result = build_diagnostic_summary(
            {"hydraulic_correlation": {
                "pump_transitions": [{
                    "return_response": {"response_status": "observed_both_windows"},
                }],
                "valve_movements": [],
            }},
            {"series": {"return_c": {"samples": 20, "gaps_over_5m": 0}}},
        )
        finding = next(item for item in result["findings"]
                       if item["code"] == "hydraulic_event_correlation")
        self.assertEqual(finding["pump_transitions"], 1)
        self.assertEqual(finding["responses_with_sufficient_coverage"], 1)
        self.assertEqual(finding["confidence"], "timestamped_cooccurrence")
        self.assertIn("nie dowodzi przyczynowości", finding["message"])

    def test_empty_input_is_explicitly_insufficient(self):
        result = build_diagnostic_summary({}, {})
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["findings_count"], 0)

    def test_healthy_input_has_no_alerts(self):
        result = build_diagnostic_summary(
            {"hydraulics": {"low_return_episodes": []}},
            {"series": {"return_c": {"samples": 100, "gaps_over_5m": 0}}},
        )
        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
