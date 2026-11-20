"""Regression tests for five-year climate normal aggregation and cache behavior."""
import unittest
from datetime import date

from fuel_errors import FuelError
from fuel_climate_provider import climate_monthly


class ClimateProviderTests(unittest.TestCase):
    def deps(self, **overrides):
        cache = {"at": 0.0, "data": None}
        calls = []
        payload = {"daily": {
            "time": ["2026-01-01", "2026-01-02", "2026-02-01"],
            "temperature_2m_mean": [2, 4, 6],
            "shortwave_radiation_sum": [1, None, 3],
            "wind_speed_10m_max": [10, 20, None],
        }}
        def request(url, params):
            calls.append((url, params))
            return payload
        deps = {
            "today": date(2026, 11, 20),
            "years": 5,
            "latitude": 52.74,
            "longitude": 23.58,
            "timezone": "Europe/Warsaw",
            "archive_url": "https://archive.example",
            "cache": cache,
            "cache_seconds": 3600,
            "request_json": request,
            "fuel_error": FuelError,
            "monotonic": lambda: 100.0,
        }
        deps.update(overrides)
        return deps, cache, calls

    def test_aggregates_monthly_means_and_preserves_sample_count(self):
        deps, cache, calls = self.deps()
        result = climate_monthly(**deps)
        self.assertEqual(result[1], {
            "t_mean": 3.0, "solar_mj": 1.0, "wind_max_kmh": 15.0, "samples": 2
        })
        self.assertEqual(result[2], {
            "t_mean": 6.0, "solar_mj": 3.0, "wind_max_kmh": None, "samples": 1
        })
        self.assertEqual(cache, {"at": 100.0, "data": result})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["start_date"], "2021-11-19")
        self.assertEqual(calls[0][1]["end_date"], "2026-11-19")

    def test_fresh_cache_skips_network(self):
        deps, cache, calls = self.deps()
        cache.update(at=99.0, data={1: {"t_mean": 5.0}})
        self.assertEqual(climate_monthly(**deps), cache["data"])
        self.assertEqual(calls, [])

    def test_empty_archive_raises_legacy_502_without_cache_write(self):
        deps, cache, _ = self.deps(
            request_json=lambda *_: {"daily": {"time": [], "temperature_2m_mean": []}}
        )
        with self.assertRaises(FuelError) as caught:
            climate_monthly(**deps)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.message, "Brak danych klimatycznych Open-Meteo dla lokalizacji")
        self.assertIsNone(cache["data"])


if __name__ == "__main__":
    unittest.main()
