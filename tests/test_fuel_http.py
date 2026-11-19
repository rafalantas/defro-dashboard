"""Unit tests for fuel upstream HTTP transport and error translation."""
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from fuel_errors import FuelError
from fuel_http import request_json


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class FuelHttpTests(unittest.TestCase):
    def test_get_request_encodes_params_and_parses_json(self):
        with patch("fuel_http.urlopen", return_value=FakeResponse(b'{"feeds": []}')) as open_url:
            result = request_json("https://api.thingspeak.com/channels/1/feeds.json",
                                  params={"results": 8000}, timeout=12)
        self.assertEqual(result, {"feeds": []})
        request, timeout = open_url.call_args.args
        self.assertIn("results=8000", request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 12)

    def test_form_request_uses_post(self):
        with patch("fuel_http.urlopen", return_value=FakeResponse(b'{"entry_id": 42}')) as open_url:
            result = request_json("https://api.thingspeak.com/update.json",
                                  form={"field1": 15}, timeout=4)
        self.assertEqual(result["entry_id"], 42)
        request, _timeout = open_url.call_args.args
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("application/x-www-form-urlencoded", request.get_header("Content-type"))

    def test_http_errors_are_translated_without_query_credentials(self):
        error = HTTPError("https://example.test/path?api_key=secret", 429, "limited", {}, None)
        with patch("fuel_http.urlopen", side_effect=error):
            with self.assertRaises(FuelError) as caught:
                request_json("https://example.test/path", params={"api_key": "secret"})
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.message, "https://example.test/path: HTTP 429")
        self.assertNotIn("secret", caught.exception.message)

    def test_network_errors_are_translated(self):
        with patch("fuel_http.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(FuelError) as caught:
                request_json("https://example.test/path")
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("offline", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
