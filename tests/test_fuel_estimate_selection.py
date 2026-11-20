"""The legacy hopper estimate precedence is an explicit, regression-tested contract."""
import unittest

from fuel_estimates import select_usage_estimate


class EstimateSelectionTests(unittest.TestCase):
    def select(self, **overrides):
        args = {
            "counter_kg": None,
            "counter_collecting": False,
            "counter_method": "licznik tymczasowy",
            "burner_kg": None,
            "burner_calibration_days": 0,
            "model_kg": None,
            "fallback_kg": None,
        }
        args.update(overrides)
        return select_usage_estimate(**args)

    def test_counter_has_highest_priority_and_preserves_provisional_label(self):
        result = self.select(counter_kg=18.0, counter_collecting=True,
                             burner_kg=20.0, burner_calibration_days=30,
                             model_kg=22.0, fallback_kg=24.0)
        self.assertEqual(result, (18.0, "licznik tymczasowy"))

    def test_calibrated_burner_precedes_house_model_after_seven_days(self):
        result = self.select(burner_kg=20.0, burner_calibration_days=7,
                             model_kg=22.0, fallback_kg=24.0)
        self.assertEqual(result, (20.0, "moc palnika skalibrowana do dosypek"))

    def test_house_model_precedes_unseasoned_burner(self):
        result = self.select(burner_kg=20.0, burner_calibration_days=6,
                             model_kg=22.0, fallback_kg=24.0)
        self.assertEqual(result, (22.0, "model domu z dosypek i temperatur"))

    def test_burner_is_used_when_model_is_unavailable(self):
        result = self.select(burner_kg=20.0, burner_calibration_days=2,
                             fallback_kg=24.0)
        self.assertEqual(result, (20.0, "moc palnika skalibrowana do dosypek"))

    def test_recent_refill_period_is_last_fallback(self):
        self.assertEqual(self.select(fallback_kg=24.0),
                         (24.0, "średnia z ostatniego okresu"))

    def test_no_available_source_is_explicitly_unavailable(self):
        self.assertEqual(self.select(), (None, None))


if __name__ == "__main__":
    unittest.main()
