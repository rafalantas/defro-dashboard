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

    def test_long_gap_splits_events_without_interpolation(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"return_c": [
            (start, 52.0), (start + timedelta(minutes=6), 52.0),
        ]}
        result = build_raw_diagnostics(points, start)
        self.assertEqual(result["hydraulics"]["low_return_episode_count"], 2)
        self.assertEqual(result["series"]["return_c"]["gaps_over_5m"], 1)

    def test_empty_data_is_safe(self):
        result = build_raw_diagnostics({}, datetime.now(timezone.utc))
        self.assertEqual(result["firing_activity"]["episodes_count"], 0)
        self.assertIsNone(result["hydraulics"]["return_c"]["median"])
        self.assertEqual(result["hydraulics"]["low_return_episode_count"], 0)


if __name__ == "__main__":
    unittest.main()
