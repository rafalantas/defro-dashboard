"""ThingSpeak hopper-counter provider regressions."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fuel_counter_provider import fetch_counter_points


class CounterProviderTests(unittest.TestCase):
    def test_counter_changes_are_deduplicated_and_last_known_value_extended(self):
        tz = ZoneInfo("Europe/Warsaw")
        day = date(2026, 11, 19)
        feeds = [
            {"created_at": "2026-11-19T10:00:00+01:00", "field4": "70"},
            {"created_at": "2026-11-19T10:01:00+01:00", "field4": "70"},
            {"created_at": "2026-11-19T10:02:00+01:00", "field4": "65"},
            {"created_at": "2026-11-19T10:03:00+01:00", "field4": ""},
        ]
        request = Mock(return_value={"feeds": feeds})
        cache = {}
        args = dict(
            day=day, today=date(2026, 11, 20), cache=cache,
            channel_id="valve", field_number=4, read_key="secret-read",
            endpoint="https://example.invalid", tz=tz, request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value),
        )
        points = fetch_counter_points(**args)
        self.assertEqual([value for _stamp, value in points], [70.0, 65.0, 65.0])
        self.assertEqual(points[-1][0], datetime.fromisoformat(
            "2026-11-19T10:03:00+01:00"
        ).timestamp())
        self.assertIs(fetch_counter_points(**args), cache["2026-11-19"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0],
                         "https://example.invalid/channels/valve/fields/4.json")

    def test_raw_samples_are_available_for_coverage_checks(self):
        tz = ZoneInfo("Europe/Warsaw")
        day = date(2026, 11, 19)
        start = datetime.fromisoformat("2026-11-19T10:00:00+01:00")
        feeds = [
            {"created_at": start.isoformat(), "field4": "70"},
            {"created_at": (start + timedelta(minutes=5)).isoformat(), "field4": "70"},
            {"created_at": (start + timedelta(minutes=10)).isoformat(), "field4": "65"},
        ]
        request = Mock(return_value={"feeds": feeds})
        cache = {}
        raw = fetch_counter_points(
            day=day, today=date(2026, 11, 20), cache=cache,
            channel_id="valve", field_number=4, read_key="",
            endpoint="https://example.invalid", tz=tz,
            request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value),
            include_unchanged=True,
        )

        self.assertEqual([value for _stamp, value in raw], [70.0, 70.0, 65.0])
        self.assertEqual([value for _stamp, value in cache[day.isoformat()]], [70.0, 65.0])
        request.assert_called_once()

    def test_no_channel_returns_empty_without_network(self):
        request = Mock()
        result = fetch_counter_points(
            day=date(2026, 11, 19), today=date(2026, 11, 20), cache={},
            channel_id="", field_number=4, read_key="", endpoint="https://example.invalid",
            tz=timezone.utc, request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value),
        )
        self.assertEqual(result, [])
        request.assert_not_called()

    def test_today_counter_points_are_not_cached(self):
        request = Mock(return_value={"feeds": [
            {"created_at": "2026-11-20T10:00:00Z", "field4": "50"},
        ]})
        cache = {}
        fetch_counter_points(
            day=date(2026, 11, 20), today=date(2026, 11, 20), cache=cache,
            channel_id="valve", field_number=4, read_key="", endpoint="https://example.invalid",
            tz=timezone.utc, request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
        )
        self.assertNotIn("2026-11-20", cache)


if __name__ == "__main__":
    unittest.main()
