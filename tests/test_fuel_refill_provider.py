"""ThingSpeak refill provider cache and write-contract regressions."""
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from fuel_errors import FuelError
from fuel_refill_provider import fetch_refills, write_refill


class RefillProviderTests(unittest.TestCase):
    def test_read_cache_returns_copies_and_respects_ttl(self):
        cache = {"at": 0.0, "data": None}
        request = Mock(return_value={"feeds": [{"entry_id": 1}]})
        normalize = Mock(return_value=[{"id": 1, "kg": 30.0}])
        args = dict(
            channel_id="fuel", read_key="read-key", endpoint="https://example.invalid",
            request_json=request, cache=cache, ttl_seconds=60,
            monotonic=lambda: 10.0, normalize=normalize, tz=timezone.utc,
            parse_timestamp=lambda value: value,
        )
        first = fetch_refills(**args)
        first[0]["kg"] = 999
        second = fetch_refills(**args)
        self.assertEqual(second, [{"id": 1, "kg": 30.0}])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(normalize.call_count, 1)
        self.assertEqual(request.call_args.args[0], "https://example.invalid/channels/fuel/feeds.json")
        self.assertEqual(request.call_args.args[1]["api_key"], "read-key")

    def test_read_cache_expires_and_missing_channel_is_empty(self):
        cache = {"at": 0.0, "data": [{"id": 1}]}
        request = Mock(return_value={"feeds": []})
        result = fetch_refills(
            channel_id="fuel", read_key="", endpoint="https://example.invalid",
            request_json=request, cache=cache, ttl_seconds=5,
            monotonic=lambda: 10.0, normalize=lambda feeds, tz, parse: [],
            tz=timezone.utc, parse_timestamp=lambda value: value,
        )
        self.assertEqual(result, [])
        request.assert_called_once()
        self.assertEqual(fetch_refills(
            channel_id="", read_key="", endpoint="https://example.invalid",
            request_json=request, cache=cache, ttl_seconds=5,
            monotonic=lambda: 10.0, normalize=lambda *args: [],
            tz=timezone.utc, parse_timestamp=lambda value: value,
        ), [])

    def test_write_uses_utc_timestamp_and_invalidates_caches(self):
        request = Mock(return_value={"entry_id": 123})
        invalidate = Mock()
        created = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        result = write_refill(
            channel_id="fuel", write_key="write-key", endpoint="https://example.invalid",
            request_json=request, fields={"field1": 30}, created_at=created,
            fuel_error=FuelError, invalidate=invalidate,
        )
        self.assertEqual(result, 123)
        self.assertEqual(request.call_args.kwargs["form"]["created_at"], "2026-11-20T12:00:00Z")
        invalidate.assert_called_once()

    def test_write_preserves_unconfigured_and_rate_limit_errors(self):
        with self.assertRaises(FuelError) as missing:
            write_refill(
                channel_id="", write_key="", endpoint="https://example.invalid",
                request_json=Mock(), fields={}, created_at=None,
                fuel_error=FuelError, invalidate=Mock(),
            )
        self.assertEqual(missing.exception.status, 503)

        invalidate = Mock()
        with self.assertRaises(FuelError) as limited:
            write_refill(
                channel_id="fuel", write_key="write-key", endpoint="https://example.invalid",
                request_json=Mock(return_value={"entry_id": 0}), fields={},
                created_at=None, fuel_error=FuelError, invalidate=invalidate,
            )
        self.assertEqual(limited.exception.status, 429)
        invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
