"""Regression tests for centralized ThermoFlow runtime configuration."""
import unittest
from datetime import datetime
from unittest.mock import patch

import fuel_config as config


class FuelConfigTests(unittest.TestCase):
    def test_legacy_defaults_are_preserved(self):
        self.assertEqual(str(config.TZ), "Europe/Warsaw")
        self.assertEqual(config.FUEL_LAT, "52.74")
        self.assertEqual(config.FUEL_LON, "23.58")
        self.assertEqual(config.HDD_BASE, 14.0)
        self.assertEqual(config.PELLET_KWH_PER_KG, 4.8)
        self.assertEqual(config.BOILER_EFF, 0.88)
        self.assertEqual(config.BAG_KG, 15.0)
        self.assertEqual(config.HOPPER_KG, 228.0)
        self.assertEqual(config.HOPPER_RESERVE_PERCENT, 15.0)
        self.assertEqual(config.MODEL_BASES, [12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5])
        self.assertEqual(config.COUNTER_EPOCH.isoformat(), "2026-11-13T18:27:00+01:00")

    def test_csv_environment_parsing_preserves_empty_and_whitespace_rules(self):
        with patch.dict("os.environ", {"THERMOFLOW_TEST_CSV": " first, ,third "}, clear=False):
            self.assertEqual(config._csv_env("THERMOFLOW_TEST_CSV"), ["first", "", "third"])
        with patch.dict("os.environ", {"THERMOFLOW_TEST_CSV": "  "}, clear=False):
            self.assertEqual(config._csv_env("THERMOFLOW_TEST_CSV"), [])

    def test_fixed_hdd_base_parser(self):
        for raw in ("", "off", "AUTO", "none", "false", "0", "invalid", "7.9", "22.1"):
            self.assertIsNone(config._parse_hdd_base_fixed(raw))
        self.assertEqual(config._parse_hdd_base_fixed(" 15,5 "), 15.5)
        self.assertEqual(config._parse_hdd_base_fixed("8"), 8.0)
        self.assertEqual(config._parse_hdd_base_fixed("22"), 22.0)

    def test_epoch_parser_preserves_timezone_and_fallback(self):
        fallback = datetime.fromisoformat("2026-11-13T18:27:00+01:00")
        self.assertEqual(config._parse_epoch("invalid"), fallback)
        self.assertEqual(config._parse_epoch(""), fallback)
        self.assertEqual(config._parse_epoch("2026-11-14T12:00:00+02:00").utcoffset().total_seconds(), 7200)
        naive = config._parse_epoch("2026-11-14T12:00:00")
        self.assertEqual(str(naive.tzinfo), "Europe/Warsaw")

    def test_thingSpeak_fields_are_canonical(self):
        self.assertEqual(config.FIELD_COUNTER, 16)
        self.assertEqual(config.FIELD_OUTDOOR, 4)
        self.assertEqual(config.FIELD_POWER, 8)


if __name__ == "__main__":
    unittest.main()
