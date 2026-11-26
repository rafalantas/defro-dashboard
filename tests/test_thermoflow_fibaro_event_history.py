"""Tests for on-demand Fibaro HC3 event-history analysis."""
import unittest

from thermoflow_fibaro_event_history import analyze_fibaro_event_history


class FibaroEventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "zones": [
                {"id": 7, "name": "Salon", "properties": {"devices": [2162, 2170]}},
                {"id": 13, "name": "Hol", "properties": {"devices": [2297]}},
            ],
            "heads": [
                {"id": 2162, "properties": {"heatRequired": True}},
                {"id": 2170, "properties": {"heatRequired": False}},
                {"id": 2297, "properties": {"heatRequired": True}},
            ],
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
        self.assertEqual(result["event_audit"]["heat_required_history"]["status"], "transitions_observed")
        self.assertEqual(result["event_audit"]["heat_required_history"]["current_snapshot_state"]["true"], 2)

    def test_audits_property_event_types_and_distinguishes_missing_history_from_missing_state(self):
        events = [
            {"id": 1, "timestamp": 100, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "batteryLevel", "oldValue": 90, "newValue": 89}},
            {"id": 2, "timestamp": 101, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 9999, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 3, "timestamp": 102, "type": "SomeOtherEvent", "data": {"id": 2162}},
        ]
        result = analyze_fibaro_event_history(self.snapshot, events, 90, 130)
        audit = result["event_audit"]
        self.assertEqual(result["counts"]["events_recognized"], 0)
        self.assertEqual(audit["mapped_head_property_counts"], {"batteryLevel": 1})
        self.assertEqual(
            audit["mapped_head_event_type_counts"],
            {"DevicePropertyUpdatedEvent": 1, "SomeOtherEvent": 1},
        )
        self.assertEqual(audit["mapped_head_event_data_key_counts"]["SomeOtherEvent.id"], 1)
        self.assertIn("Exact data.id match", audit["mapped_head_event_matching_rule"])
        self.assertEqual(audit["heat_required_history"]["status"], "no_events_observed")
        self.assertEqual(audit["heat_required_history"]["current_snapshot_state"]["true"], 2)
        self.assertEqual(audit["event_type_counts"]["SomeOtherEvent"], 1)

    def test_flags_large_temperature_spike_that_quickly_recovers(self):
        events = [
            {"id": 10, "timestamp": 100, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.5, "newValue": 0.0, "unit": "C"}},
            {"id": 11, "timestamp": 200, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 0.0, "newValue": 20.6, "unit": "C"}},
        ]
        result = analyze_fibaro_event_history(self.snapshot, events, 90, 300)
        transitions = result["zones"][0]["transitions"]
        self.assertEqual(result["temperature_quality"]["suspected_transient_spikes"], 2)
        self.assertTrue(all(item["quality"] == "suspected_transient_spike" for item in transitions))
        self.assertEqual(len(transitions), 2)  # raw evidence is not deleted

    def test_does_not_flag_normal_temperature_changes(self):
        events = [
            {"id": 10, "timestamp": 100, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5, "unit": "C"}},
            {"id": 11, "timestamp": 200, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.5, "newValue": 20.8, "unit": "C"}},
        ]
        result = analyze_fibaro_event_history(self.snapshot, events, 90, 300)
        self.assertEqual(result["temperature_quality"]["suspected_transient_spikes"], 0)

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


    def test_uses_mapping_valid_at_each_event_timestamp(self):
        old_snapshot = {
            "zones": [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}],
            "heads": [],
        }
        new_snapshot = {
            "zones": [{"id": 13, "name": "Sypialnia", "properties": {"devices": [2162]}}],
            "heads": [],
        }
        history = [
            {"captured_at": 90, "payload": old_snapshot},
            {"captured_at": 105, "payload": new_snapshot},
        ]
        events = [
            {"id": 1, "timestamp": 100, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 2, "timestamp": 110, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": True, "newValue": False}},
        ]
        result = analyze_fibaro_event_history(
            new_snapshot, events, 90, 120, snapshot_history=history
        )
        zones = {zone["id"]: zone for zone in result["zones"]}
        self.assertEqual(zones[7]["transitions"][0]["zone_name"], "Salon")
        self.assertEqual(zones[13]["transitions"][0]["zone_name"], "Sypialnia")
        self.assertEqual(result["event_audit"]["historical_mapping"]["events_without_prior_snapshot"], 0)

    def test_does_not_apply_future_snapshot_to_older_events(self):
        history = [{"captured_at": 105, "payload": self.snapshot}]
        events = [
            {"id": 1, "timestamp": 100, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
        ]
        result = analyze_fibaro_event_history(
            self.snapshot, events, 90, 120, snapshot_history=history
        )
        self.assertEqual(result["counts"]["events_recognized"], 0)
        self.assertEqual(result["event_audit"]["historical_mapping"]["events_without_prior_snapshot"], 1)


if __name__ == "__main__":
    unittest.main()
