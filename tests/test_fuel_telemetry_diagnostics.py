import unittest
from datetime import datetime, timedelta, timezone
from fuel_telemetry_diagnostics import build_raw_diagnostics

class RawTelemetryDiagnosticsTests(unittest.TestCase):
    def test_flue_episodes_and_low_return_are_screened(self):
        start = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        points = {"flue_c": [(start, 35), (start + timedelta(minutes=1), 85),
                            (start + timedelta(minutes=2), 120), (start + timedelta(minutes=3), 45),
                            (start + timedelta(minutes=20), 90)],
                  "boiler_power_kw": [(start + timedelta(minutes=2), 25)],
                  "return_c": [(start, 53.5), (start + timedelta(minutes=1), 56)],
                  "valve_opening_pct": [(start, 15), (start + timedelta(minutes=1), 25)],
                  "pump_floor": [(start, 1), (start + timedelta(minutes=1), 0)]}
        result = build_raw_diagnostics(points, start)
        self.assertEqual(result["firing_activity"]["episodes_count"], 2)
        self.assertFalse(result["firing_activity"]["episodes"][0]["ignition_confirmed"])
        self.assertEqual(result["hydraulics"]["low_return_samples"], 1)
        self.assertEqual(result["hydraulics"]["reference_return_protection_c"], 55.0)

    def test_empty_data_is_safe(self):
        result = build_raw_diagnostics({}, datetime.now(timezone.utc))
        self.assertEqual(result["firing_activity"]["episodes_count"], 0)
        self.assertIsNone(result["hydraulics"]["return_c"]["median"])

if __name__ == "__main__":
    unittest.main()