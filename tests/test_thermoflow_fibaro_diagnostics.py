"""Regression tests for Fibaro-to-ThermoFlow zone demand diagnostics."""
import unittest

from thermoflow_fibaro_diagnostics import build_fibaro_zone_diagnostics


class FibaroZoneDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "schema": "fibaro-hc3-snapshot/1",
            "source": "fibaro-hc3",
            "fetched_at": "2026-11-25T11:19:40+00:00",
            "zones": [
                {
                    "id": 10, "name": "Kuchnia", "active": True, "mode": "Schedule",
                    "properties": {
                        "devices": [101, 102],
                        "currentTemperatureHeating": 20.0,
                        "currentSetpointHeating": 21.0,
                        "handSetPointHeating": 17.0,
                    },
                },
                {
                    "id": 11, "name": "Salon", "active": True, "mode": "Schedule",
                    "properties": {
                        "devices": [103],
                        "currentTemperatureHeating": 22.0,
                        "currentSetpointHeating": 21.0,
                    },
                },
                {
                    "id": 12, "name": "Sypialnia", "active": True, "mode": "Schedule",
                    "properties": {
                        "devices": [104],
                        "currentTemperatureHeating": 18.0,
                        # No active setpoint or head demand state.
                    },
                },
            ],
            "heads": [
                {"id": 101, "properties": {"heatRequired": True, "windowOpened": False}},
                {"id": 102, "properties": {"heatRequired": False, "windowOpened": True}},
                {"id": 103, "properties": {"heatRequired": False, "windowOpened": False}},
                {"id": 104, "properties": {"windowOpened": False}},
            ],
        }

    def test_zone_demand_comes_from_head_state_not_temperature_gap(self):
        result = build_fibaro_zone_diagnostics(self.snapshot)
        kitchen, living, bedroom = result["zones"]
        self.assertEqual(kitchen["heat_demand"], "required")
        self.assertEqual(kitchen["temperature_relation"], "below_active_setpoint")
        self.assertEqual(living["heat_demand"], "not_required")
        self.assertEqual(living["temperature_relation"], "at_or_above_active_setpoint")
        self.assertEqual(bedroom["heat_demand"], "unknown")
        self.assertEqual(bedroom["temperature_relation"], "unknown")

    def test_manual_setpoint_is_not_used_as_active_setpoint(self):
        snapshot = {
            "zones": [{
                "id": 1, "properties": {
                    "devices": [], "currentTemperatureHeating": 19.0,
                    "handSetPointHeating": 23.0,
                },
            }],
            "heads": [],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        self.assertIsNone(result["zones"][0]["active_setpoint_c"])
        self.assertEqual(result["zones"][0]["temperature_relation"], "unknown")

    def test_head_states_aggregate_conservatively(self):
        result = build_fibaro_zone_diagnostics(self.snapshot)
        kitchen = result["zones"][0]
        self.assertTrue(kitchen["heat_required"])
        self.assertTrue(kitchen["window_open"])
        self.assertEqual(kitchen["head_count_with_demand_state"], 2)
        self.assertEqual(result["counts"]["zones_with_heat_demand"], 1)
        self.assertEqual(result["counts"]["zones_without_heat_demand"], 1)
        self.assertEqual(result["counts"]["zones_with_unknown_heat_demand"], 1)
        self.assertFalse(result["feature_readiness"]["training_ready"])

    def test_invalid_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            build_fibaro_zone_diagnostics(None)
        with self.assertRaises(ValueError):
            build_fibaro_zone_diagnostics({"zones": []})


if __name__ == "__main__":
    unittest.main()
