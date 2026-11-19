"""Optional telemetry failures must not escape the fuel feature boundary."""
import unittest
from unittest.mock import patch

import fuel


class FuelTelemetryResilienceTests(unittest.TestCase):
    def test_unexpected_telemetry_exception_becomes_explicit_unavailable_state(self):
        with patch.object(fuel, "_telemetry_history", side_effect=RuntimeError("simulated parser failure")):
            daily, state = fuel._telemetry_daily_features(30)

        self.assertEqual(daily, {})
        self.assertEqual(state["status"], "unavailable")
        self.assertIn("RuntimeError", state["error"])
        self.assertIn("simulated parser failure", state["error"])

    def test_successful_telemetry_preserves_daily_aggregates_and_fetch_time(self):
        audit = {
            "daily": [{"day": "2026-11-18", "co_c_mean": 65.0}],
            "fetched_at": "2026-11-19T10:00:00+00:00",
        }
        with patch.object(fuel, "_telemetry_history", return_value=audit):
            daily, state = fuel._telemetry_daily_features(30)

        self.assertEqual(daily["2026-11-18"]["co_c_mean"], 65.0)
        self.assertEqual(state["status"], "available")
        self.assertEqual(state["days"], 1)
        self.assertEqual(state["fetched_at"], audit["fetched_at"])


if __name__ == "__main__":
    unittest.main()
