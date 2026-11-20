"""Unit tests for burner energy conversion and calibration."""
import unittest
from datetime import datetime, timedelta, timezone

from fuel_energy import burner_factor, burner_kg, calibrate_burner, calibration


EPOCH = datetime(2026, 11, 13, 18, 27, tzinfo=timezone.utc)


class FuelEnergyTests(unittest.TestCase):
    def test_burner_calibration_requires_minimum_days(self):
        period = {"kg": 100.0, "est_kg_raw": 125.0, "days": 6.0, "days_missing_burner": 0}
        short = calibrate_burner([period], 7.0)
        self.assertFalse(short["calibrated"])
        ready = calibrate_burner([{**period, "days": 8.0}], 7.0)
        self.assertTrue(ready["calibrated"])
        self.assertEqual(ready["factor"], 0.8)

    def test_calibration_keeps_epoch_specific_factors_and_provisional_fallback(self):
        periods = [
            {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-08T00:00:00+00:00",
             "kg": 80.0, "est_kg_raw": 100.0, "days": 8.0, "days_missing_burner": 0},
            {"start": "2026-11-15T00:00:00+00:00", "end": "2026-11-22T00:00:00+00:00",
             "kg": 70.0, "est_kg_raw": 100.0, "days": 8.0, "days_missing_burner": 0},
        ]
        result = calibration(periods, EPOCH, EPOCH + timedelta(days=10), 0.94, 7.0)
        self.assertEqual(result["old"]["factor"], 0.8)
        self.assertEqual(result["new"]["factor"], 0.7)
        self.assertTrue(result["calibrated"])
        self.assertEqual(burner_factor(result, EPOCH - timedelta(seconds=1), EPOCH), 0.8)
        self.assertEqual(burner_factor(result, EPOCH, EPOCH), 0.7)

    def test_energy_to_kg_splits_epoch_crossing_interval(self):
        calib = {
            "old": {"factor": 1.0},
            "new": {"factor": 0.5},
        }
        start, end = EPOCH - timedelta(hours=1), EPOCH + timedelta(hours=1)
        result = burner_kg(
            start, end, calib, EPOCH,
            lambda _a, _b: (0.0, 4.8, 0, 0),
            4.8, 1.0,
        )
        self.assertEqual(result, 1.5)


if __name__ == "__main__":
    unittest.main()
