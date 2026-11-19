"""ThingSpeak daily provider temperature and energy regressions."""
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fuel_thingspeak_daily_provider import detect_data_start, fetch_day


class ThingSpeakDailyProviderTests(unittest.TestCase):
    def test_daily_temperature_and_energy_integrate_sorted_power_with_gap_cap(self):
        tz = ZoneInfo("Europe/Warsaw")
        day = date(2026, 11, 19)
        feeds = [
            {"created_at": "2026-11-19T11:00:00+01:00", "field2": "8", "field3": "2"},
            {"created_at": "2026-11-19T11:01:00+01:00", "field2": "10", "field3": "4"},
            {"created_at": "2026-11-19T11:08:20+01:00", "field2": "", "field3": "6"},
        ]
        request = Mock(return_value={"feeds": feeds})
        result = fetch_day(
            day=day, tz=tz, channel_id="temps", read_key="read-key",
            endpoint="https://example.invalid", outdoor_field=2, power_field=3,
            hdd_base=14.0, max_gap_seconds=300, request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value),
        )
        self.assertEqual(result["day"], "2026-11-19")
        self.assertEqual(result["t_mean"], 9.0)
        self.assertEqual(result["hdd"], 5.0)
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["burner_kwh"], 0.37)
        self.assertEqual(request.call_args.args[0], "https://example.invalid/channels/temps/feeds.json")
        self.assertEqual(request.call_args.args[1]["api_key"], "read-key")
        self.assertEqual(request.call_args.args[1]["results"], 8000)

    def test_missing_daily_values_return_null_temperature_and_zero_energy(self):
        result = fetch_day(
            day=date(2026, 11, 19), tz=timezone.utc, channel_id="temps",
            read_key="", endpoint="https://example.invalid", outdoor_field=2,
            power_field=3, hdd_base=14.0, max_gap_seconds=300,
            request_json=Mock(return_value={"feeds": []}),
            parse_timestamp=lambda value: datetime.fromisoformat(value),
        )
        self.assertIsNone(result["t_mean"])
        self.assertIsNone(result["hdd"])
        self.assertEqual(result["burner_kwh"], 0.0)
        self.assertEqual(result["samples"], 0)

    def test_detect_data_start_uses_configured_date_without_network(self):
        request = Mock()
        result = detect_data_start(
            configured_start="2026-01-02", channel_id="temps", read_key="",
            endpoint="https://example.invalid", request_json=request,
            parse_timestamp=lambda value: datetime.fromisoformat(value),
            timezone=timezone.utc,
        )
        self.assertEqual(result, date(2026, 1, 2))
        request.assert_not_called()

    def test_detect_data_start_converts_channel_creation_to_local_date(self):
        result = detect_data_start(
            configured_start="", channel_id="temps", read_key="read-key",
            endpoint="https://example.invalid",
            request_json=Mock(return_value={"channel": {
                "created_at": "2026-01-01T23:30:00Z",
            }}),
            parse_timestamp=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
            timezone=ZoneInfo("Europe/Warsaw"),
        )
        self.assertEqual(result, date(2026, 1, 2))


if __name__ == "__main__":
    unittest.main()
