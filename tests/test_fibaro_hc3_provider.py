"""Regression tests for the read-only Fibaro HC3 provider."""
import base64
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from fibaro_hc3_provider import HC3Config, HC3Error, FibaroHC3Provider, DEFAULT_HISTORY_WINDOW_SECONDS


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class HC3ProviderTests(unittest.TestCase):
    def config(self):
        return HC3Config("http://hc3.local", "reader", "secret", 5)

    def test_auth_and_all_requests_are_get(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse([])

        provider = FibaroHC3Provider(self.config(), opener=opener)
        provider.get_devices()
        request, timeout = requests[0]
        expected = base64.b64encode(b"reader:secret").decode("ascii")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Basic {expected}")
        self.assertEqual(timeout, 5)

    def test_reads_verified_hc3_endpoints(self):
        paths = []

        def opener(request, timeout):
            paths.append(urlsplit(request.full_url).path)
            path = urlsplit(request.full_url).path
            if path == "/api/events/history":
                return FakeResponse([])
            if path == "/api/devices/123":
                return FakeResponse({"id": 123})
            if path == "/api/panels/climate/6":
                return FakeResponse({"id": 6, "properties": {}})
            return FakeResponse([])

        provider = FibaroHC3Provider(self.config(), opener=opener)
        self.assertEqual(provider.get_devices(), [])
        self.assertEqual(provider.get_device(123), {"id": 123})
        self.assertEqual(provider.get_climate_zones(), [])
        self.assertEqual(provider.get_climate_zone(6)["id"], 6)
        self.assertEqual(provider.get_events_window(100, 200), [])
        self.assertEqual(paths, [
            "/api/devices", "/api/devices/123", "/api/panels/climate",
            "/api/panels/climate/6", "/api/events/history",
        ])

    def test_history_query_uses_unix_from_to_and_limit(self):
        calls = []

        def opener(request, timeout):
            calls.append(parse_qs(urlsplit(request.full_url).query))
            return FakeResponse([])

        FibaroHC3Provider(self.config(), opener=opener).get_events_window(100, 200)
        self.assertEqual(calls[0], {"from": ["100"], "to": ["200"], "numberOfRecords": ["1000"]})

    def test_busy_history_window_is_split_and_events_are_deduplicated(self):
        calls = []

        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            start, end = int(query["from"][0]), int(query["to"][0])
            calls.append((start, end))
            if (start, end) == (0, 900):
                return FakeResponse([{"id": i, "timestamp": i} for i in range(800)])
            return FakeResponse([
                {"id": start + 1, "timestamp": start + 1},
                {"id": 9999, "timestamp": 9000},
            ])

        provider = FibaroHC3Provider(self.config(), opener=opener)
        result = provider.get_events_range(0, 900, window_seconds=900)
        self.assertEqual(calls, [(0, 900), (0, 450), (450, 900)])
        self.assertEqual([item["id"] for item in result], [1, 451])
        self.assertTrue(all(0 <= item["timestamp"] <= 900 for item in result))

    def test_default_history_window_is_six_hours_to_reduce_backfill_requests(self):
        calls = []
        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            calls.append((int(query["from"][0]), int(query["to"][0])))
            return FakeResponse([])
        FibaroHC3Provider(self.config(), opener=opener).get_events_range(
            1000, 1000 + DEFAULT_HISTORY_WINDOW_SECONDS + 10
        )
        self.assertEqual(DEFAULT_HISTORY_WINDOW_SECONDS, 6 * 60 * 60)
        self.assertEqual(calls, [
            (1000, 1000 + 6 * 60 * 60),
            (1000 + 6 * 60 * 60, 1000 + 6 * 60 * 60 + 10),
        ])

    def test_range_deduplicates_overlapping_window_boundaries(self):
        def opener(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            start = int(query["from"][0])
            return FakeResponse([
                {"id": 1, "timestamp": 10},
                {"id": 2, "timestamp": 20},
            ])

        result = FibaroHC3Provider(self.config(), opener=opener).get_events_range(
            0, 20, window_seconds=10, split_threshold=100
        )
        self.assertEqual([item["id"] for item in result], [1, 2])

    def test_credentials_can_be_loaded_from_file_without_logging_them(self):
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8") as handle:
            json.dump({"url": "http://hc3.local", "username": "reader", "password": "secret"}, handle)
            handle.flush()
            config = HC3Config.from_env({"FIBARO_HC3_CREDENTIALS_FILE": handle.name})
        self.assertEqual(config.base_url, "http://hc3.local")
        self.assertEqual(config.username, "reader")
        self.assertEqual(config.password, "secret")

    def test_missing_credentials_fail_closed(self):
        with self.assertRaises(HC3Error) as caught:
            HC3Config.from_env({})
        self.assertEqual(caught.exception.status, 503)

    def test_invalid_ids_and_ranges_are_rejected(self):
        provider = FibaroHC3Provider(self.config(), opener=lambda *_args, **_kwargs: FakeResponse({}))
        for invalid in (0, -1, True, "x"):
            with self.subTest(invalid=invalid), self.assertRaises(HC3Error):
                provider.get_device(invalid)
        for start, end in ((1, 1), (2, 1), (True, 3)):
            with self.subTest(start=start, end=end), self.assertRaises(HC3Error):
                provider.get_events_window(start, end)

    def test_path_outside_hc3_api_is_rejected(self):
        provider = FibaroHC3Provider(self.config())
        with self.assertRaises(HC3Error):
            provider._get_json("/admin/devices")


if __name__ == "__main__":
    unittest.main()
