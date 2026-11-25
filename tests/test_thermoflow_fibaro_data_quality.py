"""Tests for fail-closed Fibaro event-history quality gates."""
import unittest

from thermoflow_fibaro_data_quality import assess_fibaro_history_quality


class FibaroDataQualityTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "zones": [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}],
            "heads": [{"id": 2162, "properties": {"heatRequired": True}}],
        }
        self.events = [
            {"id": day + 1, "timestamp": 86400 * day + 100,
             "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired",
                      "oldValue": bool(day % 2), "newValue": not bool(day % 2)}}
            for day in range(7)
        ]

    def assess(self, events=None, **kwargs):
        return assess_fibaro_history_quality(
            self.events if events is None else events, self.snapshot,
            start=1, end=8 * 86400, minimum_events=7,
            minimum_observed_days=7, **kwargs
        )

    def test_complete_sparse_event_stream_can_pass_without_inventing_gaps(self):
        result = self.assess()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["training_gate"]["eligible"])
        self.assertEqual(result["coverage"]["semantics"], "sparse_event_stream")
        self.assertFalse(result["coverage"]["quiet_intervals_are_gaps"])
        self.assertFalse(result["coverage"]["timeline_completeness_claimed"])
        self.assertFalse(result["training_gate"]["promotion_enabled"])

    def test_duplicate_ids_block_training(self):
        result = self.assess(self.events + [self.events[0]])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["training_gate"]["eligible"])
        self.assertEqual(result["counts"]["duplicates"], 1)

    def test_malformed_and_out_of_window_records_block_training(self):
        result = self.assess(self.events + [{"id": 90, "timestamp": 9999999, "data": {}}])
        self.assertEqual(result["status"], "blocked")
        self.assertGreater(result["counts"]["malformed"], 0)

    def test_low_event_volume_remains_collecting_not_ready(self):
        result = self.assess(self.events[:2])
        self.assertEqual(result["status"], "collecting")
        self.assertFalse(result["training_gate"]["eligible"])

    def test_implausible_temperature_blocks_training(self):
        event = {"id": 99, "timestamp": 86400, "type": "ClimateZoneTemperatureChangedEvent",
                 "data": {"id": 7, "oldValue": 20.0, "newValue": 99.0}}
        result = self.assess(self.events + [event])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("implausible_temperature_values", [x["code"] for x in result["issues"]])

    def test_empty_snapshot_fails_closed(self):
        result = assess_fibaro_history_quality(
            self.events, {"zones": [], "heads": []},
            start=1, end=8 * 86400, minimum_events=1, minimum_observed_days=1
        )
        self.assertEqual(result["status"], "blocked")

    def test_quality_analysis_supports_collected_windows_longer_than_seven_days(self):
        events = [
            {"id": day + 1, "timestamp": 86400 * day + 100,
             "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired",
                      "oldValue": bool(day % 2), "newValue": not bool(day % 2)}}
            for day in range(8)
        ]
        result = assess_fibaro_history_quality(
            events, self.snapshot, start=1, end=9 * 86400,
            minimum_events=8, minimum_observed_days=8
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["counts"]["recognized_events"], 8)
        self.assertEqual(result["counts"]["transitions"], 8)

    def test_rejects_invalid_window(self):
        with self.assertRaises(ValueError):
            assess_fibaro_history_quality(self.events, self.snapshot,
                                          start=100, end=50)


if __name__ == "__main__":
    unittest.main()
