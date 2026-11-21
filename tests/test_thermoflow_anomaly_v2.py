import unittest

from thermoflow_anomaly_v2 import screen_series


def points(latest=25):
    return [(1000 + i * 60, 20) for i in range(12)] + [(1720, latest)]


class AnomalyV2Tests(unittest.TestCase):
    def test_process_deviation_has_numeric_evidence(self):
        result = screen_series(points(), as_of=1720, minimum_deviation=2)
        self.assertEqual(result["process_state"], "review_deviation")
        self.assertEqual(result["evidence"]["deviation"], 5)

    def test_constant_baseline_does_not_flag_small_sensor_change(self):
        result = screen_series(points(20.1), as_of=1720, minimum_deviation=2)
        self.assertEqual(result["process_state"], "within_baseline")

    def test_missing_stale_and_ambiguous_data_do_not_raise_process_alerts(self):
        for samples, now, expected in (
            ([], 1720, "missing"),
            (points() + [(3001, 99)], 3000, "stale"),
            (points() + [(1720, 25)], 1720, "ambiguous_latest"),
            (points(None), 1720, "ambiguous_latest"),
            (points() + [(1719, float("nan"))], 1720, "insufficient_contiguous_history"),
            (points() + [(2100, 25)], 2100, "insufficient_contiguous_history"),
        ):
            with self.subTest(expected=expected):
                result = screen_series(samples, as_of=now, minimum_deviation=2)
                self.assertEqual(result["data_state"], expected)
                self.assertEqual(result["process_state"], "unknown")
                self.assertIsNone(result["evidence"])
