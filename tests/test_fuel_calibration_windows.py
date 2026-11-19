"""Regression tests for consistent summary calibration windows."""
import unittest
from unittest.mock import call, patch

import fuel


class SummaryCalibrationWindowTests(unittest.TestCase):
    def test_summary_uses_full_history_for_calibration_and_recent_window_for_display(self):
        refills = [{"id": index, "kg": 15.0} for index in range(10)]
        all_periods = [{"window": "all"}]
        recent_periods = [{"window": "recent"}]
        calibration = {"source": "all-history"}
        counter = {"source": "all-refills"}

        with patch.object(fuel, "_periods", side_effect=[all_periods, recent_periods]) as periods, \
             patch.object(fuel, "_calibration", return_value=calibration) as calibrate, \
             patch.object(fuel, "_counter_calibration", return_value=counter) as counter_calibrate:
            result = fuel._summary_calibration_inputs(refills)

        self.assertEqual(result, (recent_periods, calibration, counter))
        self.assertEqual(periods.call_args_list, [call(refills), call(refills[-7:])])
        calibrate.assert_called_once_with(all_periods)
        counter_calibrate.assert_called_once_with(refills)


if __name__ == "__main__":
    unittest.main()
