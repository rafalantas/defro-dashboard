"""Tests for the stable engine boundary and legacy compatibility adapter."""
import unittest
from types import SimpleNamespace

import fuel_engine
from fuel_errors import FuelError


class FakeImplementation:
    def __init__(self):
        self.calls = []

    def handle(self, *args):
        self.calls.append(("handle", args))
        return 201, {"ok": True}

    def warmup(self):
        self.calls.append(("warmup",))


class FuelEngineInterfaceTests(unittest.TestCase):
    def test_adapter_preserves_handle_arguments_and_result(self):
        implementation = FakeImplementation()
        engine = fuel_engine.LegacyFuelEngine(implementation)
        headers = {"X-Api-Token": "not-a-real-token"}
        result = engine.handle("POST", "/api/fuel/refill", {}, headers, b'{"kg":15}')
        self.assertEqual(result, (201, {"ok": True}))
        self.assertEqual(implementation.calls, [
            ("handle", ("POST", "/api/fuel/refill", {}, headers, b'{"kg":15}'))
        ])

    def test_adapter_delegates_warmup(self):
        implementation = FakeImplementation()
        fuel_engine.LegacyFuelEngine(implementation).warmup()
        self.assertEqual(implementation.calls, [("warmup",)])

    def test_load_uses_injected_importer_and_returns_adapter(self):
        implementation = FakeImplementation()
        requested = []
        engine = fuel_engine.load(lambda name: requested.append(name) or implementation)
        self.assertIsInstance(engine, fuel_engine.LegacyFuelEngine)
        self.assertEqual(requested, ["fuel"])

    def test_load_prefers_modular_engine_factory_when_available(self):
        engine = object()
        implementation = SimpleNamespace(create_engine=lambda: engine)
        self.assertIs(fuel_engine.load(lambda _name: implementation), engine)

    def test_modular_engine_preserves_route_payloads_and_authentication(self):
        calls = []
        handlers = {
            "stats": lambda query: {"query": query},
            "summary": lambda: {"summary": True},
            "daily": lambda query: {"days": query},
            "telemetry_audit": lambda query: {"audit": query},
            "refills": lambda: [{"id": 1}],
            "add_refill": lambda data: calls.append(("add", data)) or {"id": 2},
            "void_refill": lambda event_id: {"voided": event_id},
        }
        engine = fuel_engine.ThermoFlowEngine(
            handlers=handlers,
            check_token=lambda headers: calls.append(("auth", headers)),
            fuel_error=FuelError,
            warmup=lambda: calls.append(("warmup",)),
        )
        query = {"days": ["7"]}
        self.assertEqual(engine.handle("GET", "/api/fuel/stats", query, {}, b""),
                         (200, {"query": query}))
        self.assertEqual(engine.handle("GET", "/api/fuel/summary/", {}, {}, b""),
                         (200, {"summary": True}))
        self.assertEqual(engine.handle("GET", "/api/fuel/refills", {}, {}, b""),
                         (200, {"refills": [{"id": 1}]}))
        headers = {"X-Api-Token": "test"}
        self.assertEqual(engine.handle("POST", "/api/fuel/refill", {}, headers, b'{"kg":15}'),
                         (201, {"id": 2}))
        self.assertEqual(calls, [("auth", headers), ("add", {"kg": 15})])
        self.assertEqual(engine.handle("DELETE", "/api/fuel/refill/42", {}, headers, b""),
                         (200, {"voided": "42"}))
        engine.warmup()
        self.assertEqual(calls[-1], ("warmup",))
        self.assertEqual(engine.handle("GET", "/api/fuel/not-a-route", {}, {}, b""),
                         (404, {"error": "Nie znaleziono"}))
        self.assertIsNone(engine.handle("GET", "/api/status", {}, {}, b""))

    def test_modular_engine_keeps_malformed_json_and_auth_error_contracts(self):
        engine = fuel_engine.ThermoFlowEngine(
            handlers={"add_refill": lambda _data: {}},
            check_token=lambda _headers: None,
            fuel_error=FuelError,
            warmup=lambda: None,
        )
        self.assertEqual(engine.handle("POST", "/api/fuel/refill", {}, {}, b"{"),
                         (400, {"error": "Treść musi być poprawnym JSON"}))

        def unauthorized(_headers):
            raise FuelError(401, "Brak lub zły token")
        engine._check_token = unauthorized
        self.assertEqual(engine.handle("POST", "/api/fuel/refill", {}, {}, b'{}'),
                         (401, {"error": "Brak lub zły token"}))

    def test_load_propagates_import_failure_for_api_to_report_unavailable(self):
        def fail(_name):
            raise ImportError("missing optional provider")
        with self.assertRaisesRegex(ImportError, "missing optional provider"):
            fuel_engine.load(fail)


if __name__ == "__main__":
    unittest.main()
