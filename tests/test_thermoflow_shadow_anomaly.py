import unittest

from thermoflow_shadow_anomaly import score_latest_sample


class ThermoFlowShadowAnomalyTests(unittest.TestCase):
    def test_spike_is_flagged_against_prior_contiguous_baseline(self):
        start = 1795161600
        points = [[start + index * 60, 60.0] for index in range(12)]
        points.append([start + 12 * 60, 75.0])
        result = score_latest_sample(points)
        self.assertEqual(result["status"], "scored")
        self.assertTrue(result["anomaly"])
        self.assertEqual(result["baseline_samples"], 12)
        self.assertEqual(result["mode"], "shadow_only")

    def test_gap_resets_baseline_instead_of_being_bridged(self):
        start = 1795161600
        points = [[start + index * 60, 60.0] for index in range(12)]
        points.append([start + 1000, 75.0])
        result = score_latest_sample(points)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "insufficient_contiguous_baseline")

    def test_latest_sample_is_not_used_in_its_own_baseline(self):
        start = 1795161600
        points = [[start + index * 60, 60.0] for index in range(12)]
        points.append([start + 12 * 60, 60.0])
        result = score_latest_sample(points)
        self.assertEqual(result["baseline_samples"], 12)
        self.assertFalse(result["anomaly"])

    def test_duplicate_latest_timestamp_is_excluded(self):
        start = 1795161600
        points = [[start + index * 60, 60.0] for index in range(12)]
        points.extend([[start + 11 * 60, 70.0], [start + 12 * 60, 60.0]])
        result = score_latest_sample(points)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "insufficient_contiguous_baseline")


if __name__ == "__main__":
    unittest.main()
