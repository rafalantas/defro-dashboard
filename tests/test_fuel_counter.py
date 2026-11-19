"""Unit tests for pure feeder counter calibration and epoch splitting."""
import unittest
from datetime import date, datetime, timedelta, timezone

from fuel_counter import calibrate_pairs, counter_kg, counter_regime, span_estimate


EPOCH = datetime(2026, 11, 13, 18, 27, tzinfo=timezone.utc)


class FuelCounterTests(unittest.TestCase):
    def test_calibration_uses_only_complete_nonempty_intervals(self):
        pairs = [
            ({"ts": "2026-11-01T00:00:00+00:00"}, {"ts": "2026-11-02T00:00:00+00:00", "kg": 50.0}),
            ({"ts": "2026-11-02T00:00:00+00:00"}, {"ts": "2026-11-03T00:00:00+00:00", "kg": 20.0}),
        ]
        outcomes = iter([(25.0, True, False), (30.0, True, True)])
        result = calibrate_pairs(pairs, lambda _a, _b: next(outcomes), 20)
        self.assertEqual(result["kg_per_percent"], 2.0)
        self.assertEqual(result["periods_used"], 1)
        self.assertEqual(result["kg"], 50.0)

    def test_span_estimate_splits_cross_epoch_interval(self):
        refills = [
            {"ts": "2026-11-13T17:00:00+00:00", "kg": 0.0},
            {"ts": "2026-11-13T20:00:00+00:00", "kg": 50.0},
        ]
        old = {"calibrated": True, "kg_per_percent": 2.0}
        result = span_estimate(
            refills, old, EPOCH,
            lambda start, end: (20.0, True, False) if start < EPOCH else (10.0, True, False),
            10, 0.10,
        )
        self.assertEqual(result["kg_per_percent"], 1.0)
        self.assertEqual(result["low"], 0.9)
        self.assertEqual(result["high"], 1.1)

    def test_counter_kg_uses_each_epoch_coefficient(self):
        counter = {
            "old": {"calibrated": True, "kg_per_percent": 2.0},
            "new_effective": {"kg_per_percent": 1.5},
        }
        start = EPOCH - timedelta(hours=1)
        end = EPOCH + timedelta(hours=1)
        result = counter_kg(
            start, end, counter, EPOCH,
            lambda _a, _b: (10.0, True, False),
        )
        self.assertEqual(result, (35.0, True, False))

    def test_day_regime_marks_the_epoch_day(self):
        def bounds(day):
            start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            return start, start + timedelta(days=1)
        self.assertEqual(counter_regime(date(2026, 11, 12), bounds, EPOCH), "old")
        self.assertEqual(counter_regime(date(2026, 11, 13), bounds, EPOCH), "epoch")
        self.assertEqual(counter_regime(date(2026, 11, 14), bounds, EPOCH), "new")


if __name__ == "__main__":
    unittest.main()
