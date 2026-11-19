import unittest

import fuel_telemetry


class FuelTelemetryTests(unittest.TestCase):
    def test_audit_reports_empty_series_and_daily_values(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field1": "60", "field4": "4.5"},
            {"created_at": "2026-11-18T10:00:40Z", "field1": "62", "field4": "5.0"},
        ]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 2)
        self.assertEqual(result["quality"]["series_count"], 16)
        self.assertEqual(result["quality"]["available_series"], 2)
        self.assertIn("thermoflow-valve4d-return", result["quality"]["empty_series"])
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertEqual(row["co_c_mean"], 61.0)
        self.assertEqual(row["outdoor_c_mean"], 4.75)

    def test_valve4d_pump_is_audited_but_has_no_runtime_total(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field4": "1", "field5": "1"},
            {"created_at": "2026-11-18T10:00:40Z", "field4": "1", "field5": "1"},
            {"created_at": "2026-11-18T10:20:00Z", "field4": "1", "field5": "0"},
        ]
        result = fuel_telemetry.analyze([[], feeds], ["123", "456"], 2)
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
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 1)
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertEqual(row["room_c_mean"], 21.1)
        self.assertEqual(row["room_c_samples"], 1)
        self.assertEqual(row["room_c_invalid_samples"], 1)
        room_series = next(item for item in result["series"] if item["id"] == "thermoflow-room")
        self.assertEqual(room_series["samples"], 2)  # surowa telemetria pozostaje widoczna

    def test_bad_values_are_not_counted(self):
        feeds = [{"created_at": "2026-11-18T10:00:00Z", "field1": "", "field2": "nan"}]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 1)
        self.assertEqual(result["quality"]["available_series"], 0)


if __name__ == "__main__":
    unittest.main()
