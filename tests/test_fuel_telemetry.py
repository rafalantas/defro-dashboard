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

    def test_pump_runtime_ignores_long_gaps(self):
        feeds = [
            {"created_at": "2026-11-18T10:00:00Z", "field4": "1"},
            {"created_at": "2026-11-18T10:00:40Z", "field4": "1"},
            {"created_at": "2026-11-18T10:20:00Z", "field4": "0"},
        ]
        result = fuel_telemetry.analyze([[], feeds], ["123", "456"], 2)
        row = next(item for item in result["daily"] if item["day"] == "2026-11-18")
        self.assertAlmostEqual(row["pump_valve4d_runtime_h"], 40 / 3600, places=4)
        self.assertAlmostEqual(row["pump_valve4d_known_h"], 40 / 3600, places=4)

    def test_bad_values_are_not_counted(self):
        feeds = [{"created_at": "2026-11-18T10:00:00Z", "field1": "", "field2": "nan"}]
        result = fuel_telemetry.analyze([feeds, []], ["123", "456"], 1)
        self.assertEqual(result["quality"]["available_series"], 0)


if __name__ == "__main__":
    unittest.main()
