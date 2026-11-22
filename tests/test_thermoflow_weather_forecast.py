import unittest
from datetime import datetime, timezone

from thermoflow_weather_forecast import project_weather_forecast


NOW = 1_810_000_000


def summary():
    return {
        "fetched_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        "model": {"periods_used": 42, "base_kg_per_day": 2.0},
        "daily_model": {
            "status": "aktywny", "days": 36,
            "terms": ["hdd_lag", "solar", "wind_hdd_lag"],
            "rmse_loo_kg_per_day": 1.25,
        },
        "forecast": [
            {"day": "2027-05-01", "t_mean": 8.0, "solar_mj": 12.0,
             "wind_max_kmh": 18.0, "kg": 9.25},
            {"day": "2027-05-02", "t_mean": 10.0, "solar_mj": 14.0,
             "wind_max_kmh": 12.0, "kg": 8.5},
        ],
    }


class WeatherForecastProjectionTests(unittest.TestCase):
    def test_projects_existing_planner_forecast_and_week_totals(self):
        result = project_weather_forecast(summary(), now=NOW)
        self.assertEqual(result["schema"], "thermoflow-weather-forecast/1")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["week"], {
            "days": 2, "kg": 17.8, "average_kg_per_day": 8.88,
        })
        self.assertEqual(result["model"]["kind"], "weather_daily_model")
        self.assertEqual(result["model"]["terms"], ["hdd_lag", "solar", "wind_hdd_lag"])
        self.assertEqual(result["model"]["loo_rmse_kg_per_day"], 1.25)
        self.assertEqual(result["days"][0]["solar_mj"], 12.0)

    def test_missing_summary_never_invents_a_forecast(self):
        result = project_weather_forecast(None, now=NOW)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["days"], [])
        self.assertIsNone(result["week"])

    def test_stale_summary_is_not_presented_as_live_forecast(self):
        result = project_weather_forecast(
            summary(), now=NOW + 6 * 60 * 60 + 1
        )
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["days"], [])

    def test_rejects_invalid_and_duplicate_days(self):
        data = summary()
        data["forecast"].extend([
            {"day": "2027-05-01", "t_mean": 2, "kg": 1},
            {"day": "not-a-date", "t_mean": 2, "kg": 1},
            {"day": "2027-05-03", "t_mean": 2, "kg": float("nan")},
        ])
        result = project_weather_forecast(data, now=NOW)
        self.assertEqual([row["day"] for row in result["days"]],
                         ["2027-05-01", "2027-05-02"])
        self.assertEqual(result["week"]["days"], 2)

    def test_future_or_timezone_naive_summary_timestamp_fails_closed(self):
        data = summary()
        data["fetched_at"] = "2027-05-01T12:00:00"
        result = project_weather_forecast(data, now=NOW)
        self.assertEqual(result["status"], "unavailable")
        data["fetched_at"] = datetime.fromtimestamp(NOW + 1, timezone.utc).isoformat()
        result = project_weather_forecast(data, now=NOW)
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
