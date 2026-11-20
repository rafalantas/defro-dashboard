"""Contract tests for the extracted stats endpoint orchestration."""
import threading
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fuel_errors import FuelError
from fuel_stats_service import build_stats


class StatsServiceTests(unittest.TestCase):
    def deps(self, *, daily=None, refills=None, model=None, periods=None, calibration=None):
        return {
            "max_days": 120,
            "now": lambda: datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("Europe/Warsaw")),
            "lock": threading.Lock(),
            "prefetch_daily": lambda day: None,
            "get_daily": lambda day: (daily or {}).get(day.isoformat(), {
                "day": day.isoformat(), "burner_kwh": None, "hdd": None
            }),
            "refills": lambda: list(refills or []),
            "fit_model": lambda values: model,
            "periods": lambda values: list(periods or []),
            "calibration": lambda values: calibration or {"new": {"provisional": False}},
            "day_bounds": lambda day: (
                datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Europe/Warsaw")),
                datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Europe/Warsaw"))
            ),
            "pellet_kwh_per_kg": 4.8,
            "boiler_eff": 0.88,
            "burner_factor": lambda calib, moment: 1.0,
            "om_offset": lambda: {"offset": 0.0},
            "active_base": lambda: 14.0,
            "hdd_base": 14.0,
            "hdd_base_fixed": None,
            "bag_kg": 15.0,
            "counter_calibration": lambda values: {"status": "ok"},
            "counter_epoch": datetime(2026, 11, 13, 18, 27, tzinfo=ZoneInfo("Europe/Warsaw")),
            "fuel_error": FuelError,
        }

    def test_invalid_days_preserves_400_contract(self):
        with self.assertRaises(FuelError) as caught:
            build_stats({"days": ["abc"]}, self.deps())
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.message, "days musi być liczbą")

    def test_stats_payload_and_estimate_contract(self):
        deps = self.deps(
            daily={"2026-11-20": {
                "day": "2026-11-20", "burner_kwh": 8.448, "hdd": 4.0
            }},
            refills=[{"id": 1}],
            model={"base_kg_per_day": 0.2},
            periods=[{"kg": 10}],
            calibration={"new": {"provisional": True}},
        )
        result = build_stats({"days": ["1"]}, deps)
        self.assertEqual(
            set(result),
            {"model", "om_offset", "config", "calibration", "counter_calibration",
             "summary", "refills_count", "periods", "daily", "errors"},
        )
        row = result["daily"][0]
        self.assertEqual(row["est_kg"], 2.0)
        self.assertTrue(row["est_provisional"])
        self.assertEqual(row["est_kg_per_hdd"], 0.5)
        self.assertEqual(row["heating_kg_per_hdd"], 0.45)
        self.assertEqual(result["summary"]["days_with_burner_data"], 1)
        self.assertEqual(result["refills_count"], 1)

    def test_refill_fetch_failure_returns_empty_legacy_data_and_error(self):
        deps = self.deps()
        def fail():
            raise FuelError(503, "offline")
        deps["refills"] = fail
        result = build_stats({"days": ["1"]}, deps)
        self.assertEqual(result["errors"], ["offline"])
        self.assertEqual(result["refills_count"], 0)
        self.assertIsNone(result["model"])


if __name__ == "__main__":
    unittest.main()
