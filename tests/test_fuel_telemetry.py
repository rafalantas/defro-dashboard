import unittest

import fuel_telemetry
from datetime import datetime

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=fuel_telemetry.TZ)


class FuelTelemetryTests(unittest.TestCase):
    def test_audit_reports_empty_series_and_daily_values(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field1": "60", "field4": "4.5"},
            {"created_at": "2026-11-18T10:00:40Z", "field1": "62", "field4": "5.0"},
        ]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 2, now=NOW)
        self.assertEqual(result["quality"]["series_count"], 18)
        self.assertEqual(result["quality"]["available_series"], 2)
        self.assertIn("thermoflow-valve4d-return", result["quality"]["empty_series"])
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertEqual(row["co_c_mean"], 61.0)
        self.assertEqual(row["outdoor_c_mean"], 4.75)


    def test_fire_channel_keeps_only_flame_and_uses_existing_co_history(self):
        co = [
            {"created_at": "2026-11-18T10:00:00Z", "field1": "65"},
            {"created_at": "2026-11-18T10:00:40Z", "field1": "63"},
        ]
        fire = [
            {"created_at": "2026-11-18T10:00:00Z", "field1": "0", "field2": "0"},
            {"created_at": "2026-11-18T10:00:40Z", "field1": "1", "field2": "220"},
        ]
        config = {"setpoint_c": 70, "hysteresis_c": 6, "source": "live_emodul_snapshot"}
        result = fuel_telemetry.analyze([co, [], fire], ["123", "456", "789"], 2,
                                        now=NOW, ignition_config=config)
        raw = result["raw_diagnostics"]
        self.assertEqual(raw["ignition_activity"]["requests_count"], 1)
        self.assertEqual(raw["fire_activity"]["episodes_count"], 1)
        self.assertEqual(result["channels"][2]["name"], "fire_sensor")
        self.assertEqual(result["channels"][2]["feed_count"], 2)
        self.assertEqual(raw["ignition_activity"]["configuration"]["setpoint_c"], 70.0)
        self.assertFalse(raw["ignition_activity"]["configuration"]["historical_setting_changes_known"])

    def test_valve4d_pump_is_audited_but_has_no_runtime_total(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field4": "1", "field5": "1"},
            {"created_at": "2026-11-18T10:00:40Z", "field4": "1", "field5": "1"},
            {"created_at": "2026-11-18T10:20:00Z", "field4": "1", "field5": "0"},
        ]
        result = fuel_telemetry.analyze([[], feeds], ["123", "456"], 2, now=NOW)
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertNotIn("pump_valve4d_runtime_h", row)
        self.assertNotIn("pump_valve4d_known_h", row)
        self.assertEqual(row["pump_floor_runtime_h"], 0.01)  # API exposes runtime rounded to 2 decimal places
        valve_series = next(item for item in result["series"] if item["id"] == "thermoflow-pump-valve4d")
        self.assertEqual(valve_series["samples"], 3)  # surowy audyt nadal jest dostępny

    def test_zero_room_temperature_is_excluded_from_daily_calculations(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field5": "0", "field1": "60"},
            {"created_at": "2026-11-18T10:00:40Z", "field5": "21.1", "field1": "61"},
        ]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 2, now=NOW)
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertEqual(row["room_c_mean"], 21.1)
        self.assertEqual(row["room_c_samples"], 1)
        self.assertEqual(row["room_c_invalid_samples"], 1)
        room_series = next(item for item in result["series"] if item["id"] == "thermoflow-room")
        self.assertEqual(room_series["samples"], 2)  # surowa telemetria pozostaje widoczna

    def test_raw_ingestion_quality_counts_invalid_missing_ranges_and_duplicates(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field1": "65", "field2": "nan"},
            {"created_at": "2026-11-18T10:00:00Z", "field1": "140", "field2": "42"},
            {"created_at": "not-a-timestamp", "field1": "", "field2": "oops"},
        ]
        result = fuel_telemetry.analyze([feeds, [], []], ["123", "456", "789"], 2, now=NOW)
        audit = result["raw_ingestion_quality"]
        primary = audit["channels"][0]
        self.assertEqual(primary["raw_feeds"], 3)
        self.assertEqual(primary["timestamp_valid_feeds"], 2)
        self.assertEqual(primary["timestamp_invalid_feeds"], 1)
        self.assertEqual(primary["duplicate_timestamp_groups"], 1)
        self.assertEqual(primary["duplicate_timestamp_feeds"], 1)

        co = primary["fields"]["co_c"]
        self.assertEqual(co["accepted_samples"], 1)
        self.assertEqual(co["missing_samples"], 1)
        self.assertEqual(co["out_of_range_samples"], 1)
        self.assertEqual(co["lost_samples"], 2)
        self.assertEqual(co["loss_percent"], 66.7)

        dhw = primary["fields"]["dhw_c"]
        self.assertEqual(dhw["invalid_samples"], 2)
        self.assertEqual(dhw["missing_samples"], 0)
        self.assertEqual(dhw["loss_percent"], 66.7)
        self.assertEqual(primary["range_policy"], "diagnostic_only_no_samples_filtered")
        # The pre-parse audit must not deduplicate or otherwise change series.
        co_series = next(item for item in result["series"] if item["key"] == "co_c")
        self.assertEqual(co_series["samples"], 2)

    def test_raw_ingestion_quality_handles_empty_channels(self):
        result = fuel_telemetry.analyze([[], []], ["123", "456"], 1, now=NOW)
        audit = result["raw_ingestion_quality"]
        self.assertEqual(audit["total_raw_feeds"], 0)
        self.assertIsNone(audit["channels"][0]["fields"]["co_c"]["loss_percent"])
        self.assertEqual(audit["channels"][0]["timestamp_invalid_feeds"], 0)

    def test_bad_values_are_not_counted(self):
        feeds = [{"created_at": "2026-11-18T10:00:00Z", "field1": "", "field2": "nan"}]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 1)
        self.assertEqual(result["quality"]["available_series"], 0)


if __name__ == "__main__":
    unittest.main()


class FuelTelemetryAuditServiceTests(unittest.TestCase):
    def test_fetches_days_in_parallel_and_restores_chronological_order(self):
        from datetime import date
        from threading import Lock
        from time import sleep
        from zoneinfo import ZoneInfo
        from fuel_telemetry_audit_service import (
            AUDIT_MAX_WORKERS, AUDIT_REQUEST_TIMEOUT_SECONDS, build_audit,
        )

        active = 0
        max_active = 0
        lock = Lock()
        calls = []

        def request_json(url, *, params, timeout):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append((url, params, timeout))
            sleep(0.03)
            with lock:
                active -= 1
            return {"feeds": [{"created_at": params["start"], "field1": url.split("/")[-2]}]}

        captured = {}

        def analyze(feeds, channel_ids, days, ignition_config=None):
            captured.update(feeds=feeds, channel_ids=channel_ids, days=days,
                            ignition_config=ignition_config)
            return {"ok": True}

        result = build_audit(
            {"days": ["1"]}, max_days=120,
            channel_ids=["temps", "valve", "fire"],
            read_keys=["read-a", "read-b", "read-c"],
            today=date(2026, 11, 20), tz=ZoneInfo("Europe/Warsaw"),
            endpoint="https://api.thingspeak.com", request_json=request_json,
            analyze=analyze, fuel_error=Exception,
            ignition_config={"setpoint_c": 70, "hysteresis_c": 6},
        )

        self.assertEqual(len(calls), 6)
        self.assertGreater(max_active, 1)
        self.assertLessEqual(max_active, AUDIT_MAX_WORKERS)
        self.assertTrue(all(call[2] == AUDIT_REQUEST_TIMEOUT_SECONDS for call in calls))
        self.assertEqual([len(channel) for channel in captured["feeds"]], [2, 2, 2])
        self.assertEqual(captured["days"], 1)
        self.assertEqual(captured["ignition_config"], {"setpoint_c": 70, "hysteresis_c": 6})
        for channel in captured["feeds"]:
            self.assertLess(channel[0]["created_at"], channel[1]["created_at"])
        self.assertTrue(result["ok"])
        self.assertIn("fetched_at", result)

    def test_optional_empty_fire_channel_is_skipped(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from fuel_telemetry_audit_service import build_audit

        calls = []

        def request_json(url, *, params, timeout):
            calls.append(url)
            return {"feeds": []}

        result = build_audit(
            {"days": ["1"]}, max_days=120,
            channel_ids=["temps", "valve", ""], read_keys=["", "", ""],
            today=date(2026, 11, 20), tz=ZoneInfo("Europe/Warsaw"),
            endpoint="https://api.thingspeak.com", request_json=request_json,
            analyze=lambda feeds, ids, days: {"channel_feed_counts": [len(x) for x in feeds]},
            fuel_error=Exception,
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual(result["channel_feed_counts"], [0, 0, 0])
