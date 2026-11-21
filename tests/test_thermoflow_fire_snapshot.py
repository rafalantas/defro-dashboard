import io
import json
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from thermoflow_fire_snapshot import FIRE, SnapshotError, read_snapshot, summarize_snapshot
from thermoflow_observed_cycles import build_observed_cycle_dataset

NOW = datetime(2027, 1, 15, 8, tzinfo=timezone.utc)


class FireSnapshotTests(unittest.TestCase):
    def test_partition_boundaries_and_duplicates_preserve_input_quality(self):
        seen = []

        def open_response(url, *, timeout):
            parsed = urlsplit(url)
            self.assertEqual(parsed.hostname, "api.thingspeak.com")
            self.assertEqual(parsed.path, "/channels/123/fields/1.json")
            self.assertEqual(parse_qs(parsed.query)["api_key"], ["sample-key"])
            seen.append(parsed)
            feeds = [
                {"created_at": "2027-01-14T07:59:59Z", "field1": "0"},
                {"created_at": "2027-01-14T08:00:00Z", "field1": "0"},
                {"created_at": "2027-01-14T08:00:00Z", "field1": "1"},
                {"created_at": "2027-01-14T08:00:40Z", "field1": None},
            ]
            return io.BytesIO(json.dumps({"feeds": feeds}).encode())

        result = read_snapshot("123", "sample-key", days=2, now=NOW, opener=open_response)
        self.assertEqual(len(seen), 2)
        self.assertEqual([state for _, state in result["points"][FIRE]], [0, 0, 1, None])
        self.assertEqual(summarize_snapshot(result)["invalid_or_missing"], 1)
        self.assertEqual(build_observed_cycle_dataset(result)["dataset"]["status"], "collecting")
        self.assertNotIn("sample-key", json.dumps(result))

    def test_bad_credentials_do_not_echo_url_or_key(self):
        def fail(url, *, timeout):
            raise HTTPError(url, 401, "authorization", {}, None)

        with self.assertRaisesRegex(SnapshotError, "^ThingSpeak HTTP 401$"):
            read_snapshot("123", "sample-key", days=1, now=NOW, opener=fail)

    def test_rejects_truncated_window(self):
        def truncated(url, *, timeout):
            return io.BytesIO(json.dumps({"feeds": [None] * 8000}).encode())

        with self.assertRaisesRegex(SnapshotError, "truncated"):
            read_snapshot("123", "sample-key", days=1, now=NOW, opener=truncated)

    def test_lack_of_data_is_explicit(self):
        result = read_snapshot("123", "sample-key", days=1, now=NOW,
                               opener=lambda url, timeout: io.BytesIO(b'{"feeds":[]}'))
        self.assertEqual(summarize_snapshot(result)["samples"], 0)
        self.assertIsNone(summarize_snapshot(result)["last_timestamp"])
