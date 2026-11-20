"""Regression tests for refill command validation and audit semantics."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock

from fuel_errors import FuelError
from fuel_refill_service import add_refill, void_refill


class RefillCommandServiceTests(unittest.TestCase):
    def deps(self, **overrides):
        write = Mock(return_value=123)
        refills = Mock(return_value=[{"id": 10}, {"id": 11}])
        deps = {
            "bag_kg": 15.0,
            "fuel_error": FuelError,
            "tz": ZoneInfo("Europe/Warsaw"),
            "write": write,
            "now": lambda: datetime(2026, 11, 20, 12, tzinfo=ZoneInfo("Europe/Warsaw")),
            "refills_raw": refills,
        }
        deps.update(overrides)
        return deps, write, refills

    def test_add_refill_kg_and_note_contract(self):
        deps, write, _ = self.deps()
        result = add_refill({"kg": "30.5", "note": "x" * 300}, deps)
        self.assertEqual(result, {
            "id": 123, "ts": "2026-11-20T12:00:00+01:00", "kg": 30.5
        })
        write.assert_called_once_with({"field1": 30.5, "field2": "x" * 255}, None)

    def test_add_refill_bags_and_naive_timestamp_use_local_timezone(self):
        deps, write, _ = self.deps()
        result = add_refill({"bags": 2, "ts": "2026-11-20T08:30:00"}, deps)
        self.assertEqual(result["kg"], 30.0)
        self.assertEqual(result["ts"], "2026-11-20T08:30:00+01:00")
        created_at = write.call_args.args[1]
        self.assertEqual(created_at.tzinfo, ZoneInfo("Europe/Warsaw"))
        self.assertEqual(write.call_args.args[0], {"field1": 30.0})

    def test_add_refill_validation_preserves_400_errors(self):
        deps, write, _ = self.deps()
        for data, message in [
            ({}, "Podaj 'kg' albo 'bags'"),
            ({"kg": "bad"}, "Podaj 'kg' albo 'bags'"),
            ({"kg": 0}, "kg poza zakresem"),
            ({"kg": 1000.1}, "kg poza zakresem"),
            ({"kg": 10, "ts": "not-a-date"}, "Nieprawidłowy format ts (ISO 8601)"),
        ]:
            with self.subTest(data=data):
                with self.assertRaises(FuelError) as caught:
                    add_refill(data, deps)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(caught.exception.message, message)
        write.assert_not_called()

    def test_void_refill_writes_audit_field_for_single_raw_id(self):
        deps, write, refills = self.deps()
        result = void_refill("11", deps)
        self.assertEqual(result, {"voided": 11, "void_entry": 123})
        refills.assert_called_once()
        write.assert_called_once_with({"field3": 11})

    def test_void_refill_invalid_or_missing_id_preserves_status(self):
        deps, write, _ = self.deps()
        with self.assertRaises(FuelError) as invalid:
            void_refill("abc", deps)
        self.assertEqual(invalid.exception.status, 400)
        with self.assertRaises(FuelError) as missing:
            void_refill("99", deps)
        self.assertEqual(missing.exception.status, 404)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
