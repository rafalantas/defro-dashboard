""""Regression tests for the Fibaro hourly/daily shadow profile."""
import unittest

from thermoflow_fibaro_event_history import analyze_fibaro_event_history
from thermoflow_fibaro_time_profile import build_fibaro_time_profile


class FibaroTimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "zones": [
                {"id": 7, "name": "Salon", "properties": {"devices": [2162]}},
            ],
            "heads": [
                {"id": 2162, "properties": {"heatRequired": True}},
            ],
        }
        self.events = [
            {"id": 1, "timestamp": 1795633200, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": False, "newValue": True}},
            {"id": 2, "timestamp": 1795636800, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "windowOpened", "oldValue": False, "newValue": True}},
            {"id": 3, "timestamp": 1795640400, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5, "unit": "C"}},
            {"id": 4, "timestamp": 1795640460, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 20.5, "newValue": 0.0, "unit": "C"}},
            {"id": 5, "timestamp": 1795640520, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 0.0, "newValue": 20.6, "unit": "C"}},
            {"id": 6, "timestamp": 1795644000, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired", "oldValue": True, "newValue": False}},
            {"id": 7, "timestamp": 1795647600, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "windowOpened", "oldValue": True, "newValue": False}},
        ]
        self.start = 1795633000
        self.end = 1795648000

    def build(self, **kwargs):
        history = analyze_fibaro_event_history(self.snapshot, self.events, self.start, self.end)
        return build_fibaro_time_profile(self.snapshot, history, **kwargs)

    def test_aggregates_heat_window_and_clean_temperature_events(self):
        result = self.build()
        self.assertEqual(result["schema"], "thermoflow-fibaro-time-profile/1")
        self.assertEqual(result["timezone"], "Europe/Warsaw")
        self.assertEqual(result["totals"]["heat_demand_started"], 1)
        self.assertEqual(result["totals"]["heat_demand_stopped"], 1)
        self.assertEqual(result["totals"]["window_opened"], 1)
        self.assertEqual(result["totals"]["window_closed"], 1)
        self.assertEqual(result["totals"]["temperature_events"], 3)
        self.assertEqual(result["totals"]["temperature_suspected_spikes"], 2)
        self.assertEqual(result["totals"]["temperature_mean_delta_c"], 0.5)
        self.assertEqual(result["counts"]["transitions_aggregated"], 7)
        # The final event is exactly midnight in Europe/Warsaw, so the data
        # correctly spans two local calendar dates.
        self.assertEqual([item["date"] for item in result["daily"]], ["2026-11-25", "2026-11-26"])
        self.assertGreaterEqual(len(result["hourly"]), 2)
        self.assertFalse(result["ai_shadow"]["training_ready"])
        self.assertTrue(result["safety"]["read_only"])

    def test_only_observed_buckets_are_returned(self):
        sparse_events = [self.events[0], self.events[-1]]
        history = analyze_fibaro_event_history(
            self.snapshot, sparse_events, self.start, self.end
        )
        result = build_fibaro_time_profile(self.snapshot, history)
        self.assertEqual(result["counts"]["observed_hour_buckets"], 2)
        self.assertEqual(result["counts"]["observed_days"], 2)
        self.assertEqual(
            [item["date"] for item in result["daily"]],
            ["2026-11-25", "2026-11-26"],
        )
        self.assertFalse(result["coverage"]["empty_buckets_synthesized"])

    def test_does_not_infer_state_durations_or_missing_intervals(self):
        result = self.build()
        self.assertFalse(result["coverage"]["state_durations_available"])
        self.assertFalse(result["coverage"]["initial_state_available"])
        self.assertIn("not reconstructed", result["coverage"]["reason"])
        self.assertEqual(result["safety"]["return_protection"], "55°C ±1°C")

    def test_global_totals_deduplicate_head_events_fanned_out_to_multiple_zones(self):
        snapshot = {
            "zones": [
                {"id": 7, "name": "Salon", "properties": {"devices": [2162]}},
                {"id": 8, "name": "Pokój", "properties": {"devices": [2162]}},
            ],
            "heads": [{"id": 2162, "properties": {"heatRequired": True}}],
        }
        history = analyze_fibaro_event_history(
            snapshot, [self.events[0]], self.start, self.end
        )
        result = build_fibaro_time_profile(snapshot, history)
        self.assertEqual(result["counts"]["transitions_aggregated"], 2)
        self.assertEqual(result["counts"]["distinct_events_aggregated"], 1)
        self.assertEqual(result["totals"]["heat_demand_started"], 1)
        self.assertEqual(sum(z["totals"]["heat_demand_started"] for z in result["zones"]), 2)
        self.assertEqual(result["snapshot_state"]["heat_required"]["true"], 1)

    def test_rejects_invalid_history_and_timezone(self):
        with self.assertRaises(ValueError):
            build_fibaro_time_profile(self.snapshot, {})
        history = analyze_fibaro_event_history(
            self.snapshot, self.events, self.start, self.end
        )
        with self.assertRaises(ValueError):
            build_fibaro_time_profile(self.snapshot, history, timezone_name="Mars/Olympus")

    def test_empty_history_remains_unknown_without_fabricated_buckets(self):
        history = analyze_fibaro_event_history(self.snapshot, [], self.start, self.end)
        result = build_fibaro_time_profile(self.snapshot, history)
        self.assertEqual(result["counts"]["transitions_aggregated"], 0)
        self.assertEqual(result["hourly"], [])
        self.assertEqual(result["daily"], [])
        self.assertIsNone(result["totals"]["temperature_mean_delta_c"])
        self.assertFalse(result["ai_shadow"]["training_ready"])


if __name__ == "__main__":
    unittest.main()
