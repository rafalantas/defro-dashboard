"""Unit tests for the pure mathematical fuel model module."""
import unittest

import fuel
import fuel_models


class FuelModelsTests(unittest.TestCase):
    def test_legacy_facade_reexports_extracted_model_functions(self):
        self.assertIs(fuel._solve, fuel_models._solve)
        self.assertIs(fuel._fit_at, fuel_models._fit_at)
        self.assertIs(fuel._predict_daily, fuel_models._predict_daily)

    def test_lagged_temperature_keeps_existing_weights_and_missing_behavior(self):
        self.assertEqual(fuel_models._lagged_temperature(10.0, 20.0), 14.0)
        self.assertIsNone(fuel_models._lagged_temperature(10.0, None))

    def test_hdd_and_daily_prediction(self):
        self.assertEqual(fuel_models._hdd_from([(1.0, 10.0), (0.5, 16.0)], 18.0), 9.0)
        model = {
            "base_kg_per_day": 1.0,
            "kg_per_hdd": 0.5,
            "balance_temp": 20.0,
            "kg_per_solar_mj": -0.02,
            "kg_per_wind_hdd": 0.1,
        }
        self.assertEqual(fuel_models._predict_kg_per_day(model, 10.0), 6.0)
        self.assertEqual(fuel_models._predict_daily(model, 10.0, 5.0, 20.0), 6.1)

    def test_hdd_regression_preserves_legacy_coefficients(self):
        rows = [
            (1.0, 6.0, [(1.0, 10.0)]),
            (1.0, 7.0, [(1.0, 8.0)]),
            (1.0, 8.0, [(1.0, 6.0)]),
        ]
        result = fuel_models._fit_at(rows, 20.0)
        self.assertEqual(result["base_kg_per_day"], 1.0)
        self.assertEqual(result["kg_per_hdd"], 0.5)

    def test_linear_solver(self):
        self.assertAlmostEqual(fuel_models._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 6.0])[0], 1.8)
        self.assertAlmostEqual(fuel_models._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 6.0])[1], 1.4)


if __name__ == "__main__":
    unittest.main()
