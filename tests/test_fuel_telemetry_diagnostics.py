import unittest
from datetime import datetime, timedelta, timezone

from fuel_telemetry_diagnostics import build_raw_diagnostics


class RawTelemetryDiagnosticsTests(unittest.TestCase):
    def test_flue_episode_has_time_aligned_context_but_no_ignition_claim(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {
            "flue_c": [(start, 35), (start + timedelta(minutes=1), 85),
                       (start + timedelta(minutes=2), 120), (start + timedelta(minutes=3), 45),
                       (start + timedelta(minutes=20), 90)],
            "boiler_power_kw": [(start + timedelta(minutes=2), 25)],
            "return_c": [(start, 53.5), (start + timedelta(minutes=1), 56)],
            "valve_opening_pct": [(start, 15), (start + timedelta(minutes=1), 25)],
            "pump_floor": [(start, 1), (start + timedelta(minutes=1), 0)],
        }
        result = build_raw_diagnostics(points, start)
        firing = result["firing_activity"]
        self.assertEqual(firing["episodes_count"], 2)
        self.assertFalse(firing["episodes"][0]["ignition_confirmed"])
        self.assertTrue(firing["episodes"][0]["evidence"]["boiler_power_above_1kw"])
        self.assertEqual(firing["episodes"][0]["context"]["return_c"]["count"], 2)
        self.assertEqual(result["hydraulics"]["low_return_samples"], 1)
        self.assertEqual(result["hydraulics"]["reference_return_protection_c"], 55.0)

    def test_consecutive_low_return_samples_are_clustered(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"return_c": [
            (start, 53.0), (start + timedelta(minutes=1), 52.0),
            (start + timedelta(minutes=2), 51.0),
            (start + timedelta(minutes=20), 53.0),
        ]}
        result = build_raw_diagnostics(points, start)
        hydraulics = result["hydraulics"]
        self.assertEqual(hydraulics["low_return_samples"], 4)
        self.assertEqual(hydraulics["low_return_episode_count"], 2)
        self.assertEqual(hydraulics["low_return_episodes"][0]["samples"], 3)
        self.assertEqual(hydraulics["low_return_episodes"][0]["return_c"]["min"], 51.0)

    def test_recovery_closes_low_return_episode(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"return_c": [
            (start, 53.0), (start + timedelta(minutes=1), 52.0),
            (start + timedelta(minutes=2), 56.0),
            (start + timedelta(minutes=3), 53.0),
        ]}
        result = build_raw_diagnostics(points, start)
        self.assertEqual(result["hydraulics"]["low_return_episode_count"], 2)

    def test_long_gap_splits_events_without_interpolation(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"return_c": [
            (start, 52.0), (start + timedelta(minutes=6), 52.0),
        ]}
        result = build_raw_diagnostics(points, start)
        self.assertEqual(result["hydraulics"]["low_return_episode_count"], 2)
        self.assertEqual(result["series"]["return_c"]["gaps_over_5m"], 1)


    def test_fire_sensor_records_flame_appearance_and_disappearance(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {
            "fire_detected": [
                (start, 0), (start + timedelta(minutes=1), 1),
                (start + timedelta(minutes=2), 1), (start + timedelta(minutes=3), 0),
            ],
            "fire_brightness": [
                (start + timedelta(minutes=1), 210),
                (start + timedelta(minutes=2), 260),
            ],
            "flue_c": [(start, 35), (start + timedelta(minutes=1), 80)],
        }
        result = build_raw_diagnostics(points, start)
        fire = result["fire_activity"]
        self.assertEqual(fire["episodes_count"], 1)
        event = fire["episodes"][0]
        self.assertEqual(event["classification"], "sensor_reported_flame_presence")
        self.assertTrue(event["flame_appearance_transition_observed"])
        self.assertTrue(event["flame_disappearance_transition_observed"])
        self.assertFalse(event["ignition_command_confirmed"])
        self.assertEqual(event["brightness"]["max"], 260)

    def test_fire_sensor_gap_splits_episode_without_claiming_flame_off(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"fire_detected": [
            (start, 1), (start + timedelta(minutes=1), 1),
            (start + timedelta(minutes=10), 1),
        ]}
        result = build_raw_diagnostics(points, start)
        events = result["fire_activity"]["episodes"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["ended_by"], "telemetry_gap")
        self.assertFalse(events[0]["flame_disappearance_transition_observed"])
        self.assertFalse(events[0]["flame_appearance_transition_observed"])

    def test_empty_fire_series_is_safe(self):
        result = build_raw_diagnostics({}, datetime.now(timezone.utc))
        self.assertEqual(result["fire_activity"]["episodes_count"], 0)


    def test_hysteresis_crossing_starts_a_correlated_ignition_cycle(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {
            "boiler_temp_c": [
                (start, 65.0), (start + timedelta(minutes=1), 63.5),
                (start + timedelta(minutes=2), 64.0), (start + timedelta(minutes=10), 70.0),
            ],
            "boiler_setpoint_c": [(ts, 70.0) for ts in (
                start, start + timedelta(minutes=1), start + timedelta(minutes=2),
                start + timedelta(minutes=10))],
            "boiler_hysteresis_c": [(ts, 6.0) for ts in (
                start, start + timedelta(minutes=1), start + timedelta(minutes=2),
                start + timedelta(minutes=10))],
            "fire_detected": [
                (start, 0), (start + timedelta(minutes=2), 1),
                (start + timedelta(minutes=3), 1), (start + timedelta(minutes=4), 0),
            ],
            "fire_brightness": [(start + timedelta(minutes=2), 220)],
            "flue_c": [(start, 40), (start + timedelta(minutes=3), 85)],
        }
        result = build_raw_diagnostics(points, start)
        ignition = result["ignition_activity"]
        self.assertEqual(ignition["requests_count"], 1)
        self.assertEqual(ignition["requests"][0]["ignition_threshold_c"], 64.0)
        cycle = ignition["cycles"][0]
        self.assertEqual(cycle["minutes_to_flame"], 1.0)
        self.assertEqual(cycle["minutes_to_flue_activity"], 2.0)
        self.assertEqual(cycle["minutes_to_temperature_recovery"], 9.0)

    def test_hysteresis_crossing_does_not_bridge_long_telemetry_gap(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {
            "boiler_temp_c": [(start, 65), (start + timedelta(minutes=10), 63)],
            "boiler_setpoint_c": [(start, 70), (start + timedelta(minutes=10), 70)],
            "boiler_hysteresis_c": [(start, 6), (start + timedelta(minutes=10), 6)],
        }
        result = build_raw_diagnostics(points, start)
        self.assertEqual(result["ignition_activity"]["requests_count"], 0)

    def test_empty_data_is_safe(self):
        result = build_raw_diagnostics({}, datetime.now(timezone.utc))
        self.assertEqual(result["firing_activity"]["episodes_count"], 0)
        self.assertIsNone(result["hydraulics"]["return_c"]["median"])
        self.assertEqual(result["hydraulics"]["low_return_episode_count"], 0)


if __name__ == "__main__":
    unittest.main()
