"""Open-Meteo provider fallback, archive and forecast-cache regressions."""
import unittest
from datetime import date, datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fuel_errors import FuelError
from fuel_weather_provider import openmeteo_mean, weather_for_day


class WeatherProviderTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Warsaw")
        self.today = date(2026, 11, 20)
        self.now = lambda: datetime(2026, 11, 20, 12, 0, tzinfo=self.tz)

    def test_historical_temperature_uses_month_bounded_archive_and_caches(self):
        cache = {}
        request = Mock(return_value={"daily": {
            "time": ["2026-10-03", "2026-10-04"],
            "temperature_2m_mean": [7.5, None],
        }})
        value = openmeteo_mean(
            day=date(2026, 10, 3), latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            cache=cache, request_json=request, fuel_error=FuelError, now=self.now,
        )
        self.assertEqual(value, 7.5)
        self.assertEqual(cache, {"2026-10-03": 7.5})
        self.assertEqual(request.call_args.args[0], "archive")
        self.assertEqual(request.call_args.args[1]["start_date"], "2026-10-01")
        self.assertEqual(request.call_args.args[1]["end_date"], "2026-10-31")
        self.assertEqual(openmeteo_mean(
            day=date(2026, 10, 3), latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            cache=cache, request_json=Mock(side_effect=AssertionError("cache miss")),
            fuel_error=FuelError, now=self.now,
        ), 7.5)

    def test_temperature_api_failure_is_optional_and_not_cached(self):
        cache = {}
        request = Mock(side_effect=FuelError(502, "archive unavailable"))
        result = openmeteo_mean(
            day=date(2026, 10, 3), latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            cache=cache, request_json=request, fuel_error=FuelError, now=self.now,
        )
        self.assertIsNone(result)
        self.assertEqual(cache, {})

    def test_today_forecast_temperature_is_not_frozen(self):
        cache = {}
        request = Mock(return_value={"daily": {
            "time": ["2026-11-20"], "temperature_2m_mean": [4.0],
        }})
        result = openmeteo_mean(
            day=self.today, latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            cache=cache, request_json=request, fuel_error=FuelError, now=self.now,
        )
        self.assertEqual(result, 4.0)
        self.assertNotIn("2026-11-20", cache)
        self.assertEqual(request.call_args.args[0], "forecast")

    def test_weather_cache_and_future_refresh_ttl(self):
        cache = {}
        fetched = {}
        request = Mock(return_value={"daily": {
            "time": ["2026-11-20", "2026-11-21"],
            "shortwave_radiation_sum": [8.2, 9.1],
            "wind_speed_10m_max": [17.0, 20.0],
        }})
        args = dict(
            day=self.today, latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            variables="shortwave_radiation_sum,wind_speed_10m_max",
            cache=cache, fetched=fetched, cache_seconds=3600,
            request_json=request, fuel_error=FuelError, now=self.now,
            monotonic=lambda: 100.0,
        )
        self.assertEqual(weather_for_day(**args), {
            "solar_mj": 8.2, "wind_max_kmh": 17.0,
        })
        self.assertEqual(request.call_count, 1)
        self.assertEqual(weather_for_day(**args)["wind_max_kmh"], 17.0)
        self.assertEqual(request.call_count, 1)

    def test_weather_api_failure_returns_last_known_value(self):
        cache = {"2026-11-20": {"solar_mj": 5.0, "wind_max_kmh": 10.0}}
        args = dict(
            day=self.today, latitude="52.74", longitude="23.58",
            timezone=self.tz, archive_url="archive", forecast_url="forecast",
            variables="shortwave_radiation_sum,wind_speed_10m_max",
            cache=cache, fetched={}, cache_seconds=3600,
            request_json=Mock(side_effect=FuelError(502, "forecast unavailable")),
            fuel_error=FuelError, now=self.now, monotonic=lambda: 5000.0,
        )
        self.assertEqual(weather_for_day(**args), cache["2026-11-20"])

    def test_missing_coordinates_disable_weather_lookup(self):
        request = Mock()
        self.assertEqual(weather_for_day(
            day=self.today, latitude="", longitude="", timezone=self.tz,
            archive_url="archive", forecast_url="forecast", variables="all",
            cache={}, fetched={}, cache_seconds=3600, request_json=request,
            fuel_error=FuelError, now=self.now, monotonic=lambda: 0.0,
        ), {})
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
