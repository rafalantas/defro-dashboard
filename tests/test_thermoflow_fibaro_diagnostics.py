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
                        "currentTemperature": 20.0,
                        "currentTemperatureHeating": 20.5,
                        "currentSetpointHeating": 21.0,
                        "autoSetPointHysteresis": 2.0,
                        "handSetPointHeating": 17.0,
                    },
                },
                {
                    "id": 11, "name": "Salon", "active": True, "mode": "Schedule",
                    "properties": {
                        "devices": [103],
                        "currentTemperature": 22.0,
                        "currentTemperatureHeating": 21.5,
                        "currentSetpointHeating": 21.0,
                    },
                },
                {
                    "id": 12, "name": "Sypialnia", "active": True, "mode": "Schedule",
                    "properties": {
                        "devices": [104],
                        "currentTemperature": 18.0,
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
        self.assertEqual(kitchen["current_temperature_c"], 20.0)
        self.assertEqual(kitchen["current_temperature_heating_c"], 20.5)
        self.assertEqual(kitchen["temperature_gap_c"], 1.0)
        self.assertEqual(kitchen["active_setpoint_source"], "currentSetpointHeating")
        self.assertEqual(kitchen["hysteresis_c"], 2.0)
        self.assertEqual(kitchen["gap_vs_hysteresis"], "within_or_below_hysteresis")
        self.assertEqual(kitchen["temperature_relation"], "below_active_setpoint")
        self.assertEqual(living["heat_demand"], "not_required")
        self.assertEqual(living["temperature_relation"], "at_or_above_active_setpoint")
        self.assertEqual(bedroom["heat_demand"], "unknown")
        self.assertEqual(bedroom["temperature_relation"], "unknown")

    def test_current_temperature_heating_is_fallback_active_setpoint(self):
        snapshot = {
            "zones": [{
                "id": 1, "name": "Salon",
                "properties": {
                    "devices": [], "currentTemperature": 20.5,
                    "currentTemperatureHeating": 21.5,
                    "currentSetpointHeating": None,
                    "autoSetPointHysteresis": 2.0,
                },
            }],
            "heads": [],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        zone = result["zones"][0]
        self.assertEqual(zone["active_setpoint_c"], 21.5)
        self.assertEqual(zone["active_setpoint_source"], "currentTemperatureHeating")
        self.assertEqual(zone["temperature_gap_c"], 1.0)
        self.assertEqual(zone["hysteresis_c"], 2.0)
        self.assertEqual(zone["gap_vs_hysteresis"], "within_or_below_hysteresis")

    def test_manual_setpoint_is_not_used_as_active_setpoint(self):
        snapshot = {
            "zones": [{
                "id": 1, "properties": {
                    "devices": [], "handSetPointHeating": 23.0,
                },
            }],
            "heads": [],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        self.assertIsNone(result["zones"][0]["active_setpoint_c"])
        self.assertIsNone(result["zones"][0]["current_temperature_c"])
        self.assertEqual(result["zones"][0]["current_temperature_heating_c"], 19.0)
        self.assertIsNone(result["zones"][0]["active_setpoint_source"])
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
        self.assertEqual(result["heads"][0]["heat_required"], True)
        self.assertEqual(result["counts"]["heads_with_unknown_heat_demand"], 1)
        self.assertFalse(result["feature_readiness"]["training_ready"])

    def test_missing_head_state_does_not_become_zone_off(self):
        snapshot = {
            "zones": [{
                "id": 1, "name": "Kuchnia",
                "properties": {"devices": [101, 102]},
            }],
            "heads": [
                {"id": 101, "properties": {"heatRequired": False}},
            ],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        self.assertEqual(result["zones"][0]["heat_demand"], "unknown")
        self.assertIsNone(result["zones"][0]["heat_required"])

    def test_parameter3_disagreement_is_reported_without_overriding_primary_state(self):
        snapshot = {
            "zones": [{
                "id": 1, "name": "Salon",
                "properties": {"devices": [101]},
            }],
            "heads": [{
                "id": 101,
                "properties": {
                    "heatRequired": True,
                    "parameter3": 0,
                    "parameter3_heat_required": False,
                },
            }],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        self.assertEqual(result["zones"][0]["heat_demand"], "required")
        self.assertEqual(result["zones"][0]["heat_demand_consistency"], "mismatch")
        self.assertEqual(result["counts"]["heads_with_demand_source_mismatch"], 1)

    def test_moderate_regulator_status_uses_parameter2_bit6(self):
        snapshot = {
            "zones": [{"id": 1, "name": "Salon", "properties": {"devices": [101]}}],
            "heads": [{
                "id": 101,
                "properties": {
                    "parameter2": 64,
                    "parameter2_moderate_regulator": True,
                },
            }],
        }
        result = build_fibaro_zone_diagnostics(snapshot)
        self.assertEqual(result["heads"][0]["parameter2"], 64)
        self.assertTrue(result["heads"][0]["moderate_regulator"])
        self.assertEqual(result["counts"]["heads_with_moderate_regulator_enabled"], 1)
        self.assertEqual(result["counts"]["heads_with_moderate_regulator_disabled"], 0)

    def test_invalid_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            build_fibaro_zone_diagnostics(None)
        with self.assertRaises(ValueError):
            build_fibaro_zone_diagnostics({"zones": []})


if __name__ == "__main__":
    unittest.main()
