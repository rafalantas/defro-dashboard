from urllib.error import HTTPError, URLError
import unittest

from api import _api_error_status


class ApiErrorStatusTests(unittest.TestCase):
    def test_client_input_errors_are_bad_request(self):
        self.assertEqual(_api_error_status(ValueError("invalid query")), 400)

    def test_upstream_client_http_errors_are_preserved(self):
        error = HTTPError("https://example.invalid", 429, "rate limited", {}, None)
        self.assertEqual(_api_error_status(error), 429)

    def test_upstream_server_http_errors_become_bad_gateway(self):
        error = HTTPError("https://example.invalid", 503, "unavailable", {}, None)
        self.assertEqual(_api_error_status(error), 502)

    def test_timeout_is_gateway_timeout(self):
        self.assertEqual(_api_error_status(TimeoutError("timed out")), 504)

    def test_network_failure_is_service_unavailable(self):
        self.assertEqual(_api_error_status(URLError("offline")), 503)

    def test_unexpected_failure_is_internal_server_error(self):
        self.assertEqual(_api_error_status(RuntimeError("boom")), 500)


if __name__ == "__main__":
    unittest.main()
