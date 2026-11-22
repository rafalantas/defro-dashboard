import unittest

from thermoflow_digital_twin import build_digital_twin, SCHEMA


class DigitalTwinTests(unittest.TestCase):
    def rows(self, count=12, gap=30):
        return [{
            "timestamp": 1000 + i * gap,
            "boiler_c": 70 - i * 0.1,
            "return_c": 56 - i * 0.05,
            "flue_c": 95 + i * 0.2,
            "pumps": {"floor": True, "dhw": False, "valve4d": True, "circulation": False},
        } for i in range(count)]

    def test_contiguous_fresh_history_produces_bounded_observational_projection(self):
        result = build_digital_twin(self.rows(), as_of=1330)
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["status"], "observing")
        self.assertEqual(result["mode"], "read_only_observational_projection")
        self.assertIn("boiler_c", result["state"])
        projection = result["projections"]["return_c"]["15"]
        self.assertIn("value_c", projection)
        self.assertEqual(projection["band_type"], "heuristic_not_probability")
        self.assertEqual(result["safety"]["return_protection_c"], "55°C ±1°C")

    def test_missing_samples_do_not_create_projection(self):
        result = build_digital_twin(self.rows(4), as_of=1090)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["projections"], {})

    def test_gap_and_stale_data_fail_closed(self):
        rows = self.rows()
        rows[7]["timestamp"] += 100
        result = build_digital_twin(rows, as_of=1430)
        self.assertEqual(result["status"], "collecting")
        stale = build_digital_twin(self.rows(), as_of=1500)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["projections"], {})

    def test_future_and_duplicate_samples_are_ignored(self):
        rows = self.rows()
        rows.append({**rows[-1], "timestamp": 1400})
        result = build_digital_twin(rows, as_of=1330)
        self.assertEqual(result["status"], "observing")
        self.assertEqual(result["coverage"]["samples"], 12)


if __name__ == "__main__":
    unittest.main()
