"""Contract tests for provenance-preserving ThermoFlow estimates."""
import unittest

from fuel_estimates import Estimate, EstimateStatus


class EstimateContractTests(unittest.TestCase):
    def test_round_trip_shape_preserves_provenance(self):
        estimate = Estimate(
            source="feeder_counter",
            unit="kg",
            status=EstimateStatus.PROVISIONAL,
            value=42.5,
            window_start="2026-11-01T00:00:00+01:00",
            window_end="2026-11-02T00:00:00+01:00",
            generated_at="2026-11-02T00:05:00+01:00",
            uncertainty_percent=10,
            coverage_percent=98.5,
            calibration_regime="post_epoch_provisional",
            model_version="thermoflow2-contract-1",
        )
        result = estimate.to_dict()
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["source"], "feeder_counter")
        self.assertEqual(result["value"], 42.5)
        self.assertEqual(result["coverage_percent"], 98.5)
        self.assertEqual(result["calibration_regime"], "post_epoch_provisional")

    def test_unavailable_estimate_has_no_numeric_value(self):
        result = Estimate(
            source="daily_model", unit="kg/day",
            status=EstimateStatus.UNAVAILABLE, value=None,
            reason="insufficient_history",
        )
        self.assertIsNone(result.to_dict()["value"])

    def test_rejects_ambiguous_or_invalid_values(self):
        with self.assertRaises(ValueError):
            Estimate("", "kg", EstimateStatus.MEASURED, 1)
        with self.assertRaises(ValueError):
            Estimate("counter", "kg", EstimateStatus.MEASURED, float("nan"))
        with self.assertRaises(ValueError):
            Estimate("counter", "kg", EstimateStatus.PROVISIONAL, None)
        with self.assertRaises(ValueError):
            Estimate("model", "kg", EstimateStatus.UNAVAILABLE, 0)
        with self.assertRaises(ValueError):
            Estimate("model", "kg", EstimateStatus.PREDICTED, 1, coverage_percent=101)


if __name__ == "__main__":
    unittest.main()
