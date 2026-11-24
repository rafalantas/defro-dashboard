from datetime import datetime, timedelta, timezone
import unittest

from api import _history_query_datetime


class HistoryQueryDatetimeTests(unittest.TestCase):
    def test_parses_utc_z_suffix(self):
        parsed = _history_query_datetime("2026-11-08T12:00:00Z")
        self.assertEqual(parsed, datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))

    def test_parses_explicit_offset(self):
        parsed = _history_query_datetime("2026-11-08T12:00:00+01:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=1))

    def test_restores_unescaped_query_offset_with_colon(self):
        parsed = _history_query_datetime("2026-11-08T12:00:00 01:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=1))

    def test_restores_unescaped_query_offset_without_colon(self):
        parsed = _history_query_datetime("2026-11-08T12:00:00 0100")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=1))

    def test_naive_datetime_gets_local_timezone(self):
        parsed = _history_query_datetime("2026-11-08T12:00:00")
        self.assertIsNotNone(parsed.tzinfo)

    def test_rejects_empty_or_oversized_input(self):
        for value in ("", " " * 65, "x" * 65):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _history_query_datetime(value)

    def test_rejects_invalid_offset(self):
        with self.assertRaises(ValueError):
            _history_query_datetime("2026-11-08T12:00:00 0x:00")


if __name__ == "__main__":
    unittest.main()
