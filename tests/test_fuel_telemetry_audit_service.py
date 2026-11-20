"""Regression tests for the uncached per-day telemetry audit service."""
import unittest
from datetime import date
from zoneinfo import ZoneInfo
from unittest.mock import Mock

from fuel_errors import FuelError
from fuel_telemetry_audit_service import (
    AUDIT_REQUEST_TIMEOUT_SECONDS,
    build_audit,
)


class TelemetryAuditServiceTests(unittest.TestCase):
    def invoke(self, query=None, request=None, channels=None):
        request = request or Mock(return_value={"feeds": [{"entry_id": 1}]})
        analyze = Mock(return_value={"daily": [{"day": "2026-11-19"}]})
        result = build_audit(
            query or {}, max_days=120,
            channel_ids=channels or ["temps", "valve"],
            read_keys=["key-a", "", "fire-key"], today=date(2026, 11, 20),
            tz=ZoneInfo("Europe/Warsaw"), endpoint="https://example.invalid",
            request_json=request, analyze=analyze, fuel_error=FuelError,
        )
        return result, request, analyze

    def test_default_seven_days_fetches_eight_days_per_channel(self):
        result, request, analyze = self.invoke()
        self.assertEqual(request.call_count, 16)
        self.assertEqual(result["daily"], [{"day": "2026-11-19"}])
        self.assertEqual(analyze.call_args.args[1:], (["temps", "valve"], 7))
        self.assertTrue(all(
            call.kwargs["timeout"] == AUDIT_REQUEST_TIMEOUT_SECONDS
            for call in request.call_args_list
        ))
        temps_call = next(
            call for call in request.call_args_list
            if "/channels/temps/" in call.args[0]
        )
        self.assertEqual(temps_call.kwargs["params"]["results"], 8000)
        self.assertEqual(temps_call.kwargs["params"]["api_key"], "key-a")

    def test_optional_fire_channel_is_fetched_and_passed_to_analysis(self):
        result, request, analyze = self.invoke(
            channels=["temps", "valve", "fire"],
        )
        self.assertEqual(request.call_count, 24)
        self.assertEqual(analyze.call_args.args[0], [
            [{"entry_id": 1}] * 8,
            [{"entry_id": 1}] * 8,
            [{"entry_id": 1}] * 8,
        ])
        self.assertEqual(analyze.call_args.args[1], ["temps", "valve", "fire"])

    def test_optional_fire_fetch_error_does_not_fail_audit(self):
        def request(url, params=None, timeout=10):
            if "/channels/fire/" in url:
                raise FuelError(502, "ThingSpeak FIRE read denied")
            return {"feeds": [{"entry_id": 1}]}

        result, _request, analyze = self.invoke(
            {"days": ["1"]}, request=Mock(side_effect=request),
            channels=["temps", "valve", "fire"],
        )
        self.assertEqual(analyze.call_args.args[1:], (["temps", "valve", "fire"], 1))
        self.assertEqual(result["channel_fetch_status"]["fire"]["status"], "unavailable")
        self.assertEqual(result["channel_fetch_status"]["fire"]["failed_days"], [
            "2026-11-19", "2026-11-20",
        ])
        self.assertEqual(len(analyze.call_args.args[0][0]), 2)
        self.assertEqual(len(analyze.call_args.args[0][1]), 2)
        self.assertEqual(analyze.call_args.args[0][2], [])

    def test_clamps_to_fourteen_days_and_preserves_local_boundaries(self):
        _, request, _ = self.invoke({"days": ["99"]})
        self.assertEqual(request.call_count, 30)
        params = next(
            call.kwargs["params"] for call in request.call_args_list
            if "/channels/temps/" in call.args[0]
            and call.kwargs["params"]["start"] == "2026-11-06 00:00:00"
        )
        self.assertEqual(params["end"], "2026-11-07 00:00:00")
        self.assertEqual(params["timezone"], "Europe/Warsaw")

    def test_invalid_days_and_missing_channel_keep_400_503(self):
        with self.assertRaises(FuelError) as bad:
            self.invoke({"days": ["nope"]})
        self.assertEqual(bad.exception.status, 400)
        with self.assertRaises(FuelError) as missing:
            self.invoke(channels=["temps", ""])
        self.assertEqual(missing.exception.status, 503)

    def test_upstream_error_propagates_without_partial_success_payload(self):
        request = Mock(side_effect=FuelError(502, "upstream down"))
        with self.assertRaises(FuelError) as error:
            self.invoke({"days": ["1"]}, request=request)
        self.assertEqual(error.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
