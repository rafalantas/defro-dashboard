"""Tests for bounded parallel prefetch orchestration."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fuel_errors import FuelError
from fuel_prefetch_service import prefetch


class PrefetchServiceTests(unittest.TestCase):
    def deps(self, **overrides):
        calls = []
        deps = {
            "configured": lambda: True,
            "temps_data_start": lambda: date(2026, 11, 17),
            "now": lambda: datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("Europe/Warsaw")),
            "max_days": 120,
            "get_daily_raw": lambda day: calls.append(("daily", day)),
            "counter_points": lambda day: calls.append(("counter", day)),
            "fuel_error": FuelError,
            "executor": ThreadPoolExecutor,
        }
        deps.update(overrides)
        return deps, calls

    def test_prefetch_uses_start_date_and_includes_today(self):
        deps, calls = self.deps()
        prefetch(deps)
        self.assertEqual(len(calls), 8)
        self.assertEqual({day for _, day in calls}, {
            date(2026, 11, 17), date(2026, 11, 18),
            date(2026, 11, 19), date(2026, 11, 20),
        })
        self.assertEqual({kind for kind, _ in calls}, {"daily", "counter"})

    def test_prefetch_days_argument_bounds_history(self):
        deps, calls = self.deps()
        prefetch(deps, days=2)
        self.assertEqual({day for _, day in calls}, {
            date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)
        })

    def test_missing_source_does_not_stop_other_days(self):
        def daily(day):
            if day == date(2026, 11, 18):
                raise FuelError(503, "temporary")
            calls.append(("daily", day))
        deps, calls = self.deps(get_daily_raw=daily)
        prefetch(deps)
        self.assertEqual(len([x for x in calls if x[0] == "counter"]), 4)
        self.assertEqual(len([x for x in calls if x[0] == "daily"]), 3)

    def test_unconfigured_skips_all_work(self):
        deps, calls = self.deps(configured=lambda: False)
        prefetch(deps)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
