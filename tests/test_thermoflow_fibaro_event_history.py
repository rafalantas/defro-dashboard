"""Tests for on-demand Fibaro HC3 event-history analysis."""
import unittest

from thermoflow_fibaro_event_history import analyze_fibaro_event_history


class FibaroEventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "zones": [
                {"id": 7, "name": "Salon", "properties": {"devices": [2162, 2170]}},
                {"id": 13, "name": "Hol", "properties": {"devices": [2297]}},
            ]
        }

    def test_reconstructs_only_verified_head_transitions_and_maps_zones(self):
        events = [
            {"id": 1, "timestamp": 100, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 2, "timestamp": 110, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2170, "property": "windowOpened", "oldValue": False, "newValue": True}},
            {"id": 3, "timestamp": 120, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5, "unit": "C"}},
        ]
        result = analyze_fibaro_event_history(self.snapshot, events, 90, 130)
        salon = result["zones"][0]
        self.assertEqual(salon["name"], "Salon")
        self.assertEqual(salon["transition_count"], 3)
        self.assertEqual(salon["heat_required_transitions"], 1)
        self.assertEqual(salon["window_opened_transitions"], 1)
        self.assertEqual(salon["temperature_transitions"], 1)
        self.assertEqual(result["coverage"]["status"], "event_transitions_only")
        self.assertFalse(result["coverage"]["state_durations_available"])

    def test_ignores_unknown_properties_and_unmapped_devices(self):
        events = [
            {"id": 1, "timestamp": 100, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "batteryLevel", "oldValue": 90, "newValue": 89}},
            {"id": 2, "timestamp": 101, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 9999, "property": "heatRequired", "oldValue": False, "newValue": True}},
        ]
        result = analyze_fibaro_event_history(self.snapshot, events, 90, 130)
        self.assertEqual(result["counts"]["events_recognized"], 0)
        self.assertEqual(result["counts"]["events_ignored"], 2)
        self.assertEqual(result["coverage"]["status"], "no_recognized_events")

    def test_rejects_invalid_and_overlong_windows(self):
        for start, end in ((10, 10), (20, 10), (0, 7 * 86400 + 1), (True, 20)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                analyze_fibaro_event_history(self.snapshot, [], start, end)

    def test_does_not_claim_duration_or_gap_reconstruction(self):
        result = analyze_fibaro_event_history(self.snapshot, [], 90, 130)
        self.assertEqual(result["coverage"]["gaps_in_state"], "not_inferred")
        self.assertFalse(result["safety"]["raw_history_persisted"])
        self.assertFalse(result["safety"]["background_polling"])
        self.assertFalse(result["safety"]["device_commands_sent"])


if __name__ == "__main__":
    unittest.main()
