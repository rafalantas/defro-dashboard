import unittest
from thermoflow_time_alignment import align_observations, normalize_timestamp, timestamp_labels

def feature(stamp, transitions):
    return {"values": {"schema": "thermoflow-fibaro-heating-feature/2",
                       "observed_at": stamp, "transitions": transitions}}

class TimeAlignmentTests(unittest.TestCase):
    def test_iso_requires_offset_and_normalizes(self):
        self.assertEqual(normalize_timestamp("2026-11-26T18:00:00+01:00"),
                         normalize_timestamp("2026-11-26T17:00:00Z"))
        with self.assertRaises(ValueError):
            normalize_timestamp("2026-11-26T18:00:00")
    def test_local_label_has_offset(self):
        labels = timestamp_labels(normalize_timestamp("2026-01-15T12:00:00Z"))
        self.assertEqual(labels["local_date"], "2026-01-15")
        self.assertIn("+01:00", labels["local"])
    def test_nearest_real_sample_is_attached(self):
        event = feature(1000, [{"source_event_id": 7, "signal": "heat_required",
            "entity": "head", "device_id": 10, "zone_id": None,
            "historical_zone_mapping": "unverified", "old_value": False, "new_value": True}])
        samples = [{"observed_at": 1040, "source_key": "m:1040",
                    "payload": {"data": {"temperatures": {"co": 65.0}}}}]
        row = align_observations([event], samples, tolerance_seconds=60)["observations"][0]
        self.assertTrue(row["matched"])
        self.assertEqual(row["emodul"]["delta_seconds"], 40)
    def test_outside_tolerance_stays_unmatched(self):
        result = align_observations([feature(1000, [{"signal": "setpoint"}])],
            [{"observed_at": 1300, "payload": {"data": {"temperatures": {"co": 60}}}}],
            tolerance_seconds=90)
        self.assertFalse(result["observations"][0]["matched"])
        self.assertIsNone(result["observations"][0]["emodul"])
    def test_missing_telemetry_does_not_drop_fibaro(self):
        result = align_observations([feature(1000, [{"signal": "window_open"}])], [])
        self.assertEqual(result["counts"]["unmatched"], 1)
        self.assertFalse(result["semantics"]["interpolation"])

if __name__ == "__main__":
    unittest.main()
