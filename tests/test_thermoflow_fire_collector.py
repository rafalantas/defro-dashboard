import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from thermoflow_fire_collector import (
    CollectorError, WINDOW, archive_status, collect_once, export_snapshot,
    fetch_window, initialize,
)
from thermoflow_observed_cycles import build_observed_cycle_dataset

T0 = 1_800_000_000


class FireCollectorTests(unittest.TestCase):
    def test_canonical_input_exports_fire_zero_and_unknown_without_identifiers(self):
        payload = {"feeds": [
            {"entry_id": 2, "created_at": "2027-01-15T08:00:01Z", "field1": "0",
             "field2": "0"},
            {"entry_id": 3, "created_at": "2027-01-15T08:00:02Z", "field1": "bad"},
            {"entry_id": 4, "created_at": "2027-01-15T08:00:03Z", "field1": "1"},
        ]}

        def open_response(url, *, timeout):
            parsed = urlsplit(url)
            self.assertEqual(parsed.hostname, "api.thingspeak.com")
            self.assertEqual(parsed.path, "/channels/123/feeds.json")
            self.assertEqual(parse_qs(parsed.query)["api_key"], ["mock-key"])
            return io.BytesIO(json.dumps(payload).encode())

        rows = fetch_window("123", "mock-key", T0 + 1, T0 + 3, opener=open_response)
        self.assertEqual(rows, [(2, T0 + 1, 0, 0.0), (3, T0 + 2, None, None)])
        db = sqlite3.connect(":memory:")
        initialize(db)
        collect_once(db, "123", "mock-key", T0 + 3,
                     fetch=lambda ch, k, start, end: [row for row in rows if start <= row[1] < end])
        snapshot = export_snapshot(db, "123")
        self.assertEqual(snapshot["points"]["thermoflow-fire"], [(T0 + 1, 0), (T0 + 2, None)])
        self.assertEqual(build_observed_cycle_dataset(snapshot)["dataset"]["status"], "collecting")
        self.assertNotIn("mock-key", json.dumps(snapshot))

    def test_resume_after_reopen_and_no_duplicate_on_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite3"

            def fetch(ch, key, start, end):
                row = (1, T0 - 30, 0, None)
                return [row] if start <= row[1] < end else []

            db = sqlite3.connect(path)
            initialize(db)
            self.assertEqual(collect_once(db, "123", "k", T0, fetch=fetch)["samples"], 1)
            db.close()
            db = sqlite3.connect(path)
            self.assertEqual(collect_once(db, "123", "k", T0 + 300, fetch=fetch), {
                "archived_through": T0 + 300, "samples": 1,
                "valid_fire_samples": 1, "last_valid_fire_timestamp": T0 - 30,
            })
            db.close()

    def test_checkpoint_does_not_advance_through_failed_window(self):
        db = sqlite3.connect(":memory:")
        initialize(db)
        accepted_until = T0 - WINDOW * 3

        def fetch(ch, key, start, end):
            if start >= accepted_until:
                raise CollectorError("upstream failure")
            return []

        with self.assertRaises(CollectorError):
            collect_once(db, "123", "k", T0, fetch=fetch)
        self.assertEqual(archive_status(db, "123")["archived_through"], accepted_until)

    def test_upstream_auth_error_has_no_url_or_key(self):
        def fail(url, *, timeout):
            raise HTTPError(url, 401, "authorization", {}, None)

        with self.assertRaisesRegex(CollectorError, "^ThingSpeak HTTP 401$"):
            fetch_window("123", "mock-key", T0, T0 + 40, opener=fail)

    def test_truncated_window_is_not_treated_as_complete(self):
        def truncated(url, *, timeout):
            return io.BytesIO(json.dumps({"feeds": [None] * 8000}).encode())

        with self.assertRaisesRegex(CollectorError, "truncated"):
            fetch_window("123", "mock-key", T0, T0 + 40, opener=truncated)
