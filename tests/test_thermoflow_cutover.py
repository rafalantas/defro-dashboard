"""End-to-end HTTP checks for the ThermoFlow 2.0 cutover boundary."""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import api
import fuel
import fuel_engine
import thermoflow2_runtime
from fuel_errors import FuelError


class ThermoFlowCutoverHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.calls_lock = threading.Lock()

        def record(value):
            with self.calls_lock:
                self.calls.append(value)

        def check_token(headers):
            if headers.get("X-Api-Token") != "test-token":
                raise FuelError(401, "Brak lub zły token")

        self.engine = fuel_engine.ThermoFlowEngine(
            handlers={
                "stats": lambda query: {"days": query.get("days", ["60"])[0]},
                "summary": lambda: {"configured": True, "ai_shadow": {"mode": "shadow_only"}},
                "daily": lambda query: {"days": query.get("days", ["60"])[0]},
                "telemetry_audit": lambda query: {"audit": True},
                "refills": lambda: [{"id": "feed-1", "kg": 15}],
                "add_refill": lambda data: record(("add", data)) or {"saved": data},
                "void_refill": lambda event_id: record(("void", event_id)) or {"voided": event_id},
            },
            check_token=check_token,
            fuel_error=FuelError,
            warmup=lambda: record(("warmup",)),
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.previous_engine = api._fuel_engine
        api._fuel_engine = self.engine

    def tearDown(self):
        api._fuel_engine = self.previous_engine
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        connection.close()
        return status, json.loads(payload.decode("utf-8"))

    def test_legacy_alias_and_factory_resolve_to_thermoflow_engine(self):
        self.assertIs(fuel, thermoflow2_runtime)
        self.assertIsInstance(fuel_engine.load(), fuel_engine.ThermoFlowEngine)

    def test_get_routes_preserve_query_and_response_contract(self):
        status, payload = self.request("GET", "/api/fuel/stats?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"days": "7"})
        status, payload = self.request("GET", "/api/fuel/summary/")
        self.assertEqual(status, 200)
        self.assertEqual(payload["ai_shadow"]["mode"], "shadow_only")
        status, payload = self.request("GET", "/api/fuel/refills")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"refills": [{"id": "feed-1", "kg": 15}]})

    def test_mutating_routes_enforce_authentication_and_preserve_payloads(self):
        status, payload = self.request("POST", "/api/fuel/refill", {"bags": 2})
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "Brak lub zły token"})
        status, payload = self.request(
            "POST", "/api/fuel/refill", {"bags": 2}, {"X-Api-Token": "test-token"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload, {"saved": {"bags": 2}})
        self.assertIn(("add", {"bags": 2}), self.calls)
        status, payload = self.request(
            "DELETE", "/api/fuel/refill/42", headers={"X-Api-Token": "test-token"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"voided": "42"})

    def test_malformed_json_returns_client_error_without_mutation(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(
            "POST", "/api/fuel/refill", body=b"{",
            headers={"Content-Length": "1", "X-Api-Token": "test-token"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": "Treść musi być poprawnym JSON"})
        self.assertFalse(any(call[0] == "add" for call in self.calls))

    def test_unknown_fuel_route_is_a_json_404(self):
        status, payload = self.request("GET", "/api/fuel/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "Nie znaleziono"})

    def test_fuel_engine_failure_does_not_break_core_health_route(self):
        api._fuel_engine = None
        with patch.object(api, "_load_fuel_engine", return_value=None):
            status, payload = self.request("GET", "/api/fuel/summary")
            self.assertEqual(status, 503)
            self.assertIn("niedostępny", payload["error"])
            with patch.object(api, "snapshot", return_value={"configured": True, "connected": True}):
                status, payload = self.request("GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(payload["connected"])


if __name__ == "__main__":
    unittest.main()
