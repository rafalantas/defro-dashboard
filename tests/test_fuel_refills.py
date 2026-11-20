"""Unit tests for pure refill event normalization and merging."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from fuel_refills import merge_close_refills, normalize_refill_feeds


TZ = ZoneInfo("Europe/Warsaw")


def parse_timestamp(raw):
    value = str(raw).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


class FuelRefillsTests(unittest.TestCase):
    def test_normalization_sorts_events_and_excludes_voided_refill(self):
        feeds = [
            {"entry_id": 3, "created_at": "2026-11-18T12:00:00Z", "field1": "15", "field2": "voided"},
            {"entry_id": 1, "created_at": "2026-11-18T10:00:00Z", "field1": "30", "field2": "first"},
            {"entry_id": 2, "created_at": "2026-11-18T11:00:00Z", "field1": "20", "field2": "second"},
            {"entry_id": 4, "created_at": "2026-11-18T12:00:00Z", "field3": "1"},
        ]
        result = normalize_refill_feeds(feeds, TZ, parse_timestamp)
        self.assertEqual([row["id"] for row in result], [2, 3])
        self.assertEqual([row["kg"] for row in result], [20.0, 15.0])
        self.assertEqual(result[0]["note"], "second")

    def test_normalization_matches_string_csv_entry_ids_to_void_references(self):
        feeds = [
            {"entry_id": "37", "created_at": "2026-11-14T13:03:20+01:00", "field1": "6", "field2": ""},
            {"entry_id": "38", "created_at": "2026-11-14T13:03:47+01:00", "field1": "84", "field2": ""},
            {"entry_id": "39", "created_at": "2026-11-14T17:55:48+01:00", "field3": "37"},
            {"entry_id": "40", "created_at": "2026-11-14T17:56:04+01:00", "field3": "38"},
            {"entry_id": "41", "created_at": "2026-11-14T13:04:00+01:00", "field1": "90", "field2": "6+84, scalone"},
        ]

        result = normalize_refill_feeds(feeds, TZ, parse_timestamp)

        self.assertEqual([row["id"] for row in result], [41])
        self.assertEqual([row["kg"] for row in result], [90.0])

    def test_merge_close_events_preserves_all_ids_and_combines_notes(self):
        rows = [
            {"id": 10, "ts": "2026-11-18T10:00:00+01:00", "kg": 30.0, "note": "A"},
            {"id": 11, "ts": "2026-11-18T10:20:00+01:00", "kg": 15.0, "note": "B"},
            {"id": 12, "ts": "2026-11-18T10:50:00+01:00", "kg": 15.0, "note": None},
        ]
        result = merge_close_refills(rows, 1800, parse_timestamp)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["kg"], 45.0)
        self.assertEqual(result[0]["ids"], [10, 11])
        self.assertEqual(result[0]["id"], 11)
        self.assertEqual(result[0]["note"], "A + B")
        self.assertEqual(result[1]["ids"], [12])


if __name__ == "__main__":
    unittest.main()
