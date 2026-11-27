"""A cold summary must not request telemetry windows from before the channels existed."""
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import fuel

TODAY = date(2026, 11, 28)
FIRST_DAY = date(2026, 11, 12)


class WindowsBeforeDataTests(unittest.TestCase):
    def planned(self, first_day, days=120):
        with patch.object(fuel, "_temps_data_start", return_value=first_day):
            return fuel._telemetry_fetch_windows_from_data_start(days, TODAY)

    def test_windows_entirely_before_the_first_day_of_data_are_skipped(self):
        full = fuel._telemetry_fetch_windows(120, TODAY)
        kept = self.planned(FIRST_DAY)
        self.assertEqual(len(full), 41)
        self.assertLessEqual(len(kept), 7)                       # ~16 days of data in 3-day windows
        self.assertLess(len(kept), len(full) // 4)

    def test_nothing_that_could_hold_data_is_dropped(self):
        kept = self.planned(FIRST_DAY)
        day = FIRST_DAY - timedelta(days=1)                      # includes the one-day margin
        while day <= TODAY:
            self.assertTrue(any(start <= day < end for start, end, _ in kept), day)
            day += timedelta(days=1)

    def test_remaining_windows_stay_contiguous_and_end_after_today(self):
        kept = self.planned(FIRST_DAY)
        self.assertEqual(kept[-1][1], TODAY + timedelta(days=1))
        for before, after in zip(kept, kept[1:]):
            self.assertEqual(before[1], after[0])

    def test_unknown_first_day_skips_nothing(self):
        self.assertEqual(self.planned(None), fuel._telemetry_fetch_windows(120, TODAY))

    def test_failure_to_detect_the_first_day_skips_nothing(self):
        with patch.object(fuel, "_temps_data_start", side_effect=RuntimeError("probe failed")):
            windows = fuel._telemetry_fetch_windows_from_data_start(120, TODAY)
        self.assertEqual(windows, fuel._telemetry_fetch_windows(120, TODAY))

    def test_first_day_in_the_future_or_today_still_fetches_the_current_window(self):
        for first in (TODAY, TODAY + timedelta(days=3)):
            kept = self.planned(first)
            self.assertTrue(kept)                                # never an empty plan
            self.assertEqual(kept[-1][1], TODAY + timedelta(days=1))

    def test_short_history_requests_are_unchanged_when_data_covers_them(self):
        full = fuel._telemetry_fetch_windows(7, TODAY)
        self.assertEqual(self.planned(FIRST_DAY, days=7), full)

    def test_history_provider_receives_the_clipped_planner(self):
        with patch.object(fuel, "fetch_telemetry_history", return_value={}) as provider:
            fuel._telemetry_history_impl(120)
        self.assertIs(provider.call_args.kwargs["fetch_windows"], fuel._telemetry_fetch_windows_from_data_start)

    def test_the_unclipped_planner_is_unchanged(self):
        with patch.object(fuel, "_temps_data_start", return_value=FIRST_DAY):
            self.assertEqual(len(fuel._telemetry_fetch_windows(120, TODAY)), 41)


if __name__ == "__main__":
    unittest.main()
