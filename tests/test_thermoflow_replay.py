import unittest
from datetime import datetime, timezone

from thermoflow_replay import replay_telemetry


class ThermoFlowReplayTests(unittest.TestCase):
    def test_replay_is_deterministic_for_same_snapshot_and_clock(self):
        start = 1795161600
        points = {
            "thermoflow-boiler-temp": [[start, 68], [start + 60, 63], [start + 120, 62]],
            "thermoflow-flue": [[start, 45], [start + 60, 82], [start + 120, 91]],
            "thermoflow-valve4d-return": [[start, 56], [start + 60, 53]],
        }
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        first = replay_telemetry(points, now=now)
        second = replay_telemetry(points, now=now)
        self.assertEqual(first, second)
        self.assertEqual(first["input_samples"], 8)
        self.assertIn("no interpolation", first["safety"])

    def test_replay_keeps_gap_quality_visible(self):
        start = 1795161600
        points = {
            "thermoflow-flue": [[start, 80], [start + 301, 90]],
        }
        result = replay_telemetry(points, now=datetime(2026, 11, 20, tzinfo=timezone.utc))
        quality = result["thermoflow_intelligence"]["diagnostic_core"]["series"]["thermoflow-flue"]
        self.assertEqual(quality["gaps_over_5m"], 1)
        self.assertEqual(quality["coverage_status"], "review")

    def test_input_is_not_mutated(self):
        points = {"thermoflow-flue": [[1795161600, 80]]}
        original = [row[:] for row in points["thermoflow-flue"]]
        replay_telemetry(points, now=datetime(2026, 11, 20, tzinfo=timezone.utc))
        self.assertEqual(points["thermoflow-flue"], original)


if __name__ == "__main__":
    unittest.main()
