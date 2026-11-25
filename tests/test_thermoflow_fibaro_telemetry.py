"""Regression tests for the shadow-only Fibaro telemetry contract."""
import unittest
from datetime import datetime, timedelta, timezone

from thermoflow_fibaro_telemetry import build_fibaro_telemetry


class FibaroTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
        self.snapshot = {
            "schema": "fibaro-hc3-snapshot/1",
            "fetched_at": self.now.isoformat(),
            "zones": [
                {"id": 10, "name": "Kuchnia", "properties": {"devices": [101, 102]}},
                {"id": 11, "name": "Salon", "properties": {"devices": [103]}},
            ],
            "heads": [
                {"id": 101, "name": "Głowica 101", "properties": {
                    "heatRequired": True, "windowOpened": False, "batteryLevel": 80,
                    "dead": False, "parameter2": 322,
                }},
                {"id": 102, "name": "Głowica 102", "properties": {
                    "heatRequired": False, "windowOpened": True, "batteryLevel": 15,
                    "dead": True, "parameter2": 322,
                }},
                {"id": 103, "name": "Głowica 103", "properties": {
                    "windowOpened": False, "batteryLevel": 75, "dead": False,
                    "parameter2": 322,
                }},
            ],
        }
        self.diagnostics = {
            "zones": [
                {"id": 10, "name": "Kuchnia", "current_temperature_c": 19.5,
                 "active_setpoint_c": 21.0, "temperature_gap_c": 1.5,
                 "temperature_relation": "below_active_setpoint",
                 "heat_demand": "required", "heat_required": True,
                 "window_open": True, "head_count": 2, "head_count_reported": 2},
                {"id": 11, "name": "Salon", "current_temperature_c": 21.5,
                 "active_setpoint_c": 21.0, "temperature_gap_c": -0.5,
                 "temperature_relation": "at_or_above_active_setpoint",
                 "heat_demand": "unknown", "heat_required": None,
                 "window_open": False, "head_count": 1, "head_count_reported": 1},
            ],
        }

    def build(self, **kwargs):
        return build_fibaro_telemetry(
            self.snapshot, self.diagnostics, now=self.now,
            expected_head_count=3, **kwargs
        )

    def test_builds_complete_telemetry_and_shadow_features(self):
        result = self.build()
        self.assertEqual(result["schema"], "thermoflow-fibaro-telemetry/1")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["freshness"]["status"], "fresh")
        self.assertEqual(result["mapping"]["status"], "complete")
        self.assertEqual(result["mapping"]["coverage_percent"], 100.0)
        self.assertEqual(result["counts"]["heads"], 3)
        self.assertEqual(result["counts"]["heads_with_heat_demand"], 1)
        self.assertEqual(result["counts"]["heads_without_heat_demand"], 1)
        self.assertEqual(result["counts"]["heads_with_unknown_heat_demand"], 1)
        self.assertEqual(result["counts"]["heads_offline"], 1)
        self.assertEqual(result["counts"]["heads_with_low_battery"], 1)
        self.assertEqual(result["counts"]["heads_with_expected_parameter2"], 3)
        self.assertEqual(result["counts"]["zones_below_active_setpoint"], 1)
        self.assertEqual(result["ai_shadow"]["features"]["mean_temperature_gap_c"], 0.5)
        self.assertFalse(result["ai_shadow"]["training_ready"])
        self.assertFalse(result["fuel_forecast"]["applied"])
        self.assertTrue(result["safety"]["read_only"])
        self.assertFalse(result["safety"]["hc3_writes"])

    def test_missing_booleans_and_parameter_remain_unknown(self):
        snapshot = {
            **self.snapshot,
            "heads": [{"id": 101, "properties": {"batteryLevel": 50}}],
        }
        result = build_fibaro_telemetry(
            snapshot, self.diagnostics, now=self.now, expected_head_count=1
        )
        head = result["heads"][0]
        self.assertIsNone(head["heat_required"])
        self.assertIsNone(head["window_open"])
        self.assertEqual(head["communication"], "unknown")
        self.assertIsNone(head["parameter2_matches_expected"])
        self.assertEqual(result["counts"]["heads_with_unknown_heat_demand"], 1)

    def test_incomplete_and_duplicate_mapping_is_reported(self):
        snapshot = {
            **self.snapshot,
            "zones": [
                {"id": 10, "name": "Kuchnia", "properties": {"devices": [101, 102]}},
                {"id": 11, "name": "Salon", "properties": {"devices": [101, 999]}},
            ],
        }
        result = build_fibaro_telemetry(
            snapshot, self.diagnostics, now=self.now, expected_head_count=3
        )
        self.assertEqual(result["mapping"]["status"], "incomplete")
        self.assertEqual(result["mapping"]["heads_mapped_to_multiple_zones"], {"101": [10, 11]})
        self.assertEqual(result["mapping"]["unresolved_mapped_device_ids"], [999])

    def test_stale_and_future_snapshots_fail_freshness_gate(self):
        stale = {
            **self.snapshot,
            "fetched_at": (self.now - timedelta(minutes=10)).isoformat(),
        }
        stale_result = build_fibaro_telemetry(
            stale, self.diagnostics, now=self.now, expected_head_count=3
        )
        self.assertEqual(stale_result["freshness"]["status"], "stale")
        self.assertEqual(stale_result["status"], "stale")

        future = {
            **self.snapshot,
            "fetched_at": (self.now + timedelta(minutes=5)).isoformat(),
        }
        future_result = build_fibaro_telemetry(
            future, self.diagnostics, now=self.now, expected_head_count=3
        )
        self.assertEqual(future_result["freshness"]["status"], "invalid_future_timestamp")
        self.assertEqual(future_result["status"], "unknown")

    def test_parameter2_mismatch_is_degraded_not_silently_accepted(self):
        snapshot = {
            **self.snapshot,
            "heads": [
                {**head, "properties": {**head["properties"], "parameter2": 258}}
                if head["id"] == 102 else head
                for head in self.snapshot["heads"]
            ],
        }
        result = build_fibaro_telemetry(
            snapshot, self.diagnostics, now=self.now, expected_head_count=3
        )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["counts"]["heads_with_mismatched_parameter2"], 1)

    def test_invalid_inputs_and_configuration_fail_closed(self):
        with self.assertRaises(ValueError):
            build_fibaro_telemetry(None, self.diagnostics, now=self.now)
        with self.assertRaises(ValueError):
            build_fibaro_telemetry(self.snapshot, {}, now=self.now)
        with self.assertRaises(ValueError):
            self.build(expected_head_count=0)
        with self.assertRaises(ValueError):
            self.build(expected_parameter2=-1)
        with self.assertRaises(ValueError):
            build_fibaro_telemetry(
                self.snapshot, self.diagnostics,
                now=datetime(2026, 11, 25, 20, 0),
                expected_head_count=3,
            )


if __name__ == "__main__":
    unittest.main()
