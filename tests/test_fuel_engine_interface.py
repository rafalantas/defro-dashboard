"""Tests for the stable engine boundary and legacy compatibility adapter."""
import unittest

import fuel_engine


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

    def test_load_propagates_import_failure_for_api_to_report_unavailable(self):
        def fail(_name):
            raise ImportError("missing optional provider")
        with self.assertRaisesRegex(ImportError, "missing optional provider"):
            fuel_engine.load(fail)


if __name__ == "__main__":
    unittest.main()
