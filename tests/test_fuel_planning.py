import unittest
from datetime import date, datetime
from unittest.mock import patch

import fuel


class FuelPlanningTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 12, 0, tzinfo=fuel.TZ)
        self.model = {"base_kg_per_day": 2.0, "kg_per_hdd": 0.5, "balance_temp": 15.0}
        self.climate = {
            month: {"t_mean": 0.0, "solar_mj": 5.0, "wind_max_kmh": 15.0, "samples": 150}
            for month in range(1, 13)
        }
        self.forecast = [(date(2026, 11, day), 0.0) for day in range(19, 26)]

    def test_planning_returns_day_week_month_and_heating_season(self):
        with patch.object(fuel, "_climate_monthly", return_value=self.climate), \
             patch.object(fuel, "_weather", return_value={"solar_mj": 5.0, "wind_max_kmh": 15.0}), \
             patch.object(fuel, "_om_offset", return_value={"offset": 0.0}):
            result = fuel._planning_forecast(self.model, None, self.forecast, self.now, {})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["location"], "Hajnówka, Podlaskie")
        self.assertEqual(result["today"]["days"], 1)
        self.assertEqual(result["week"]["days"], 7)
        self.assertEqual(result["month"]["days"], 30)
        self.assertEqual(result["season"]["from"], "2026-11-19")
        self.assertEqual(result["season"]["to"], "2027-04-30")
        self.assertGreater(result["season"]["bags_15kg"], 0)

    def test_planning_requires_house_model(self):
        result = fuel._planning_forecast(None, None, [], self.now, {})
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
