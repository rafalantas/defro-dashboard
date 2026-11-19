"""Regression tests for the injectable ThingSpeak telemetry provider."""
import unittest
from datetime import date, timedelta
from unittest.mock import Mock

from fuel_errors import FuelError
from fuel_telemetry_provider import fetch_history


class TelemetryProviderTests(unittest.TestCase):
    def setUp(self):
        self.cache = {"key": None, "at": 0.0, "data": None}
        self.today = date(2026, 11, 19)
        self.windows = lambda days, today: [
            (today - timedelta(days=days), today, days),
            (today, today + timedelta(days=1), 0),
        ]

    def invoke(self, request, analyze=None, channel_ids=None):
        return fetch_history(
            days=2, max_days=120,
            channel_ids=channel_ids or ["temperatures", "valve"],
            read_keys=["read-a", "read-b"], today=self.today, tz=__import__("zoneinfo").ZoneInfo("Europe/Warsaw"),
            cache=self.cache, cache_ttl=21600, partial_cache_ttl=600,
            fetch_windows=self.windows, request_json=request,
            analyze=analyze or (lambda feeds, ids, days: {"daily": [], "feed_counts": [len(x) for x in feeds]}),
            endpoint="https://example.invalid", monotonic=lambda: 100.0,
        )

    def test_fetches_both_channels_and_caches_complete_result(self):
        request = Mock(return_value={"feeds": [{"entry_id": 1}]})
        result = self.invoke(request)
        self.assertEqual(result["fetch_status"], "complete")
        self.assertEqual(result["fetch_windows"], {"requested": 4, "completed": 4, "failed": 0})
        self.assertEqual(result["feed_counts"], [2, 2])
        self.assertEqual(request.call_count, 4)
        self.assertIs(self.invoke(Mock(side_effect=AssertionError("cache miss"))), result)

    def test_partial_failure_keeps_successful_windows_and_short_cache(self):
        calls = {"count": 0}

        def request(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FuelError(502, "upstream down")
            return {"feeds": [{"entry_id": calls["count"]}]}

        result = self.invoke(request)
        self.assertEqual(result["fetch_status"], "partial")
        self.assertEqual(result["fetch_windows"]["failed"], 1)
        self.assertEqual(result["feed_counts"], [1, 2])
        self.assertIn("FuelError", result["fetch_errors"][0])

    def test_missing_channel_preserves_service_unavailable_contract(self):
        with self.assertRaises(FuelError) as error:
            self.invoke(Mock(), channel_ids=["temperatures", ""])
        self.assertEqual(error.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
