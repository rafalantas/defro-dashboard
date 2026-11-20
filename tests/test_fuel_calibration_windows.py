"""Regression tests for the legacy summary calibration window."""
import unittest
from unittest.mock import call, patch

import fuel


class SummaryCalibrationWindowTests(unittest.TestCase):
    def test_summary_preserves_last_seven_refills_for_calibration_and_display(self):
        refills = [{"id": index, "kg": 15.0} for index in range(10)]
        recent_periods = [{"window": "recent"}]
        calibration = {"source": "recent-periods"}
        counter = {"source": "recent-refills"}

        with patch.object(fuel, "_periods", return_value=recent_periods) as periods, \
             patch.object(fuel, "_calibration", return_value=calibration) as calibrate, \
             patch.object(fuel, "_counter_calibration", return_value=counter) as counter_calibrate:
            result = fuel._summary_calibration_inputs(refills)

        self.assertEqual(result, (recent_periods, calibration, counter))
        periods.assert_called_once_with(refills[-7:])
        calibrate.assert_called_once_with(recent_periods)
        counter_calibrate.assert_called_once_with(refills[-7:])


if __name__ == "__main__":
    unittest.main()
