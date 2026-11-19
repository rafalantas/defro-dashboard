"""Regression tests for the extracted refill-period analysis contract."""
import unittest
from datetime import datetime, timezone

from fuel_period_analysis import build_periods


class PeriodAnalysisTests(unittest.TestCase):
    epoch = datetime(2026, 11, 13, 18, 27, tzinfo=timezone.utc)

    def build(self, refills, *, sums=None, drop=None, model=None, max_periods=50):
        return build_periods(
            refills,
            period_sums=sums or (lambda _start, _end: (12.5, 48.0, 0, 0)),
            counter_drop=drop or (lambda _start, _end: (10.0, True, False)),
            model=model,
            active_base=lambda: 14.0,
            max_periods=max_periods,
            max_days=120,
            pellet_kwh_per_kg=4.8,
            boiler_efficiency=0.88,
            counter_epoch=self.epoch,
        )

    def test_preserves_legacy_period_fields_and_calculations(self):
        rows = self.build([
            {"ts": "2026-11-01T00:00:00+00:00", "kg": 0},
            {"ts": "2026-11-04T00:00:00+00:00", "kg": 60},
        ], model={"base_kg_per_day": 2.0, "kg_per_hdd": 1.5})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["days"], 3.0)
        self.assertEqual(row["kg_per_day"], 20.0)
        self.assertEqual(row["hdd"], 12.5)
        self.assertEqual(row["burner_kwh"], 48.0)
        self.assertEqual(row["est_kg_raw"], 11.4)
        self.assertEqual(row["counter_drop_percent"], 10.0)
        self.assertEqual(row["kg_per_counter_percent"], 6.0)
        self.assertEqual(row["counter_regime"], "old")
        self.assertEqual(row["heating"], {"kg": 54.0, "kg_per_hdd": 4.32, "model_kg": 24.8})

    def test_missing_measurements_and_counter_floor_remain_null(self):
        rows = self.build([
            {"ts": "2026-11-15T00:00:00+00:00", "kg": 0},
            {"ts": "2026-11-18T00:00:00+00:00", "kg": 45},
        ], sums=lambda *_: (0.0, 0.0, 2, 3),
           drop=lambda *_: (18.0, True, True))
        row = rows[0]
        self.assertIsNone(row["burner_kwh"])
        self.assertIsNone(row["est_kg_raw"])
        self.assertIsNone(row["kg_per_hdd"])
        self.assertEqual(row["counter_drop_percent"], 18.0)
        self.assertIsNone(row["kg_per_counter_percent"])
        self.assertEqual(row["counter_regime"], "new")

    def test_rejects_invalid_and_overlong_periods(self):
        rows = self.build([
            {"ts": "2026-01-01T00:00:00+00:00", "kg": 0},
            {"ts": "2026-01-01T00:00:00+00:00", "kg": 5},
            {"ts": "2026-06-01T00:00:00+00:00", "kg": 10},
        ])
        self.assertEqual(rows, [])

    def test_limits_work_to_latest_configured_refill_history(self):
        refills = [
            {"ts": f"2026-01-{day:02d}T00:00:00+00:00", "kg": day}
            for day in range(1, 8)
        ]
        rows = self.build(refills, max_periods=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kg"], 6)
        self.assertEqual(rows[1]["kg"], 7)

    def test_epoch_crossing_period_is_never_assigned_one_regime(self):
        rows = self.build([
            {"ts": "2026-11-13T00:00:00+00:00", "kg": 0},
            {"ts": "2026-11-14T00:00:00+00:00", "kg": 20},
        ])
        self.assertEqual(rows[0]["counter_regime"], "epoch")


if __name__ == "__main__":
    unittest.main()
