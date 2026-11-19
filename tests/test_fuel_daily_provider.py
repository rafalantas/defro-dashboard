"""Daily ThingSpeak/Open-Meteo source arbitration regressions."""
import unittest
from datetime import date
from unittest.mock import Mock

from fuel_errors import FuelError
from fuel_daily_provider import get_daily_raw


class DailyProviderTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 11, 19)
        self.today = date(2026, 11, 20)
        self.cache = {}

    def invoke(self, **overrides):
        args = dict(
            day=self.day, today=self.today, cache=self.cache,
            channel_configured=True, data_start=lambda: date(2026, 1, 1),
            fetch_thingspeak=lambda day: {
                "day": day.isoformat(), "t_mean": 5.0, "hdd": 9.0,
                "burner_kwh": 12.0, "samples": 1000,
            },
            openmeteo_mean=lambda day: 7.0, hdd_base=14.0,
            fuel_error=FuelError,
        )
        args.update(overrides)
        return get_daily_raw(**args)

    def test_thingSpeak_is_primary_and_historical_result_is_cached(self):
        fetch = Mock(return_value={
            "day": "2026-11-19", "t_mean": 5.0, "hdd": 9.0,
            "burner_kwh": 12.0, "samples": 1000,
        })
        result = self.invoke(fetch_thingspeak=fetch)
        self.assertEqual(result["source"], "thingspeak")
        self.assertEqual(result["burner_kwh"], 12.0)
        self.assertEqual(self.cache["2026-11-19"]["hdd"], 9.0)
        result["t_mean"] = 99
        cached = self.invoke(fetch_thingspeak=Mock(side_effect=AssertionError("cache miss")))
        self.assertEqual(cached["t_mean"], 5.0)

    def test_openmeteo_fills_missing_temperature_but_keeps_burner_energy(self):
        result = self.invoke(fetch_thingspeak=lambda day: {
            "day": day.isoformat(), "t_mean": None, "hdd": None,
            "burner_kwh": 12.0, "samples": 100,
        })
        self.assertEqual(result["source"], "open-meteo")
        self.assertEqual(result["t_mean"], 7.0)
        self.assertEqual(result["hdd"], 7.0)
        self.assertEqual(result["burner_kwh"], 12.0)
        self.assertEqual(result["samples"], 100)

    def test_thingSpeak_failure_uses_openmeteo_without_caching_and_warns(self):
        result = self.invoke(fetch_thingspeak=Mock(side_effect=FuelError(502, "down")))
        self.assertEqual(result["source"], "open-meteo")
        self.assertEqual(result["warning"], "ThingSpeak niedostępny, temperatura z Open-Meteo")
        self.assertNotIn("2026-11-19", self.cache)

    def test_no_thingSpeak_channel_uses_openmeteo(self):
        fetch = Mock(side_effect=AssertionError("ThingSpeak must not be called"))
        result = self.invoke(channel_configured=False, fetch_thingspeak=fetch)
        self.assertEqual(result["source"], "open-meteo")
        fetch.assert_not_called()

    def test_missing_both_sources_is_not_cached(self):
        result = self.invoke(fetch_thingspeak=lambda day: {
            "day": day.isoformat(), "t_mean": None, "hdd": None,
            "burner_kwh": 0.0, "samples": 0,
        }, openmeteo_mean=lambda day: None)
        self.assertIsNone(result["t_mean"])
        self.assertIsNone(result["hdd"])
        self.assertNotIn("2026-11-19", self.cache)

    def test_today_is_not_cached(self):
        result = self.invoke(day=self.today)
        self.assertEqual(result["source"], "thingspeak")
        self.assertNotIn("2026-11-20", self.cache)


if __name__ == "__main__":
    unittest.main()
