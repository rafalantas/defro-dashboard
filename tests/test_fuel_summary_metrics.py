import unittest

from fuel_summary_metrics import build_refills_based, telemetry_state


class SummaryMetricsTests(unittest.TestCase):
    def test_refill_summary_uses_latest_four_heating_periods(self):
        periods = [{"hdd": 2, "kg": 9, "days": 1, "start": "skip", "end": "skip"}]
        periods += [{"hdd": 10+i, "kg": 20+i, "days": 2,
                     "start": f"start-{i}", "end": f"end-{i}"} for i in range(5)]
        result = build_refills_based(periods, {"base_kg_per_day": 3.0})
        self.assertEqual(result["periods"], 4)
        self.assertEqual(result["from"], "start-1")
        self.assertEqual(result["to"], "end-4")
        self.assertEqual(result["kg"], 90)
        self.assertEqual(result["hdd"], 50.0)
        self.assertEqual(result["kg_per_hdd"], 1.8)
        self.assertEqual(result["heating_kg_per_hdd"], 1.56)

    def test_refill_summary_without_heating_periods_is_none(self):
        self.assertIsNone(build_refills_based([{"hdd": 4, "kg": 20, "days": 2}], None))

    def test_telemetry_state_prefers_first_partial_or_unavailable_row(self):
        rows = [
            {"telemetry_status": "available", "telemetry_samples": 3},
            {"telemetry_status": "partial", "telemetry_error": "timeout", "telemetry_samples": 2},
            {"telemetry_status": "unavailable", "telemetry_error": "offline"},
        ]
        self.assertEqual(telemetry_state(rows), {"status": "partial", "error": "timeout"})

    def test_telemetry_state_counts_available_days(self):
        rows = [{"telemetry_samples": 4}, {"telemetry_samples": 0}, {}]
        self.assertEqual(telemetry_state(rows), {"status": "available", "days": 1})


if __name__ == "__main__":
    unittest.main()
