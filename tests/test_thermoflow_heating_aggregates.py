import unittest
from thermoflow_heating_aggregates import aggregate_heating_observations

def row(stamp, signal="heat_required", sample_stamp=1000, source_key="same"):
    return {"timestamp": {"unix": stamp}, "signal": signal, "matched": True,
            "emodul": {"observed_at": {"unix": sample_stamp}, "source_key": source_key,
                       "data": {"temperatures": {"co": 60.0, "outdoor": 5.0},
                                "boiler_power_kw": 22.0}}}

class HeatingAggregateTests(unittest.TestCase):
    def test_aggregates_real_observations_and_deduplicates_reused_sample(self):
        result = aggregate_heating_observations({"observations": [
            row(1795712400), row(1795712410, "setpoint")]})
        hour = result["hourly"][0]
        self.assertEqual(hour["observations"], 2)
        self.assertEqual(hour["unique_emodul_samples"], 1)
        self.assertEqual(hour["metrics"]["temperatures.co"]["count"], 2)
        self.assertEqual(hour["metrics"]["temperatures.co"]["mean"], 60.0)

    def test_unmatched_event_is_counted_without_inventing_measurements(self):
        item = {"timestamp": {"unix": 1795712400}, "signal": "window_open",
                "matched": False, "emodul": None}
        result = aggregate_heating_observations({"observations": [item]})
        self.assertEqual(result["daily"][0]["observations"], 1)
        self.assertEqual(result["daily"][0]["metrics"], {})
        self.assertEqual(result["coverage"]["empty_buckets_synthesized"], 0)

    def test_no_observations_produces_no_empty_days(self):
        result = aggregate_heating_observations({"observations": []})
        self.assertEqual(result["hourly"], [])
        self.assertEqual(result["daily"], [])

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_heating_observations({"observations": []}, timezone_name="No/Such_Zone")

if __name__ == "__main__":
    unittest.main()
