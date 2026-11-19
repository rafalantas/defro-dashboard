"""Contract tests for the canonical ThermoFlow compact ThingSpeak schema."""
import subprocess
import sys
import unittest

import api
import fuel_schema
import fuel_telemetry


class FuelSchemaTests(unittest.TestCase):
    def test_two_channels_have_eight_ordered_fields_each(self):
        self.assertEqual([channel["name"] for channel in fuel_schema.CHANNEL_FIELDS],
                         ["temperatures_power", "valve_pumps_fuel"])
        self.assertEqual([len(channel["fields"]) for channel in fuel_schema.CHANNEL_FIELDS], [8, 8])

    def test_api_and_telemetry_are_derived_from_the_same_contract(self):
        api_fields = api._thingspeak_field_schema()
        telemetry_fields = fuel_telemetry.CHANNELS
        self.assertEqual(len(api_fields), len(telemetry_fields))
        for api_channel, telemetry_channel in zip(api_fields, telemetry_fields):
            self.assertEqual(len(api_channel), len(telemetry_channel["series"]))
            for (series_id, label, _path), (_key, telemetry_label, telemetry_id, _kind) in zip(
                    api_channel, telemetry_channel["series"]):
                self.assertEqual(series_id, telemetry_id)
                self.assertEqual(label, telemetry_label)

    def test_semantic_field_numbers_are_stable_and_channel_scoped(self):
        self.assertEqual(fuel_schema.field_number("outdoor_c", "temperatures_power"), 4)
        self.assertEqual(fuel_schema.field_number("boiler_power_kw", "temperatures_power"), 8)
        self.assertEqual(fuel_schema.field_number("fuel_percent", "valve_pumps_fuel"), 8)
        self.assertEqual(fuel_schema.field_number("pump_floor", "valve_pumps_fuel"), 5)
        with self.assertRaises(KeyError):
            fuel_schema.field_number("fuel_percent", "temperatures_power")

    def test_core_api_import_does_not_import_optional_fuel_engine(self):
        script = r"""
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "fuel":
        raise AssertionError("core API attempted to import optional fuel engine")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import api
import sys
assert "fuel" not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
