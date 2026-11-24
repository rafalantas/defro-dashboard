import unittest
from unittest.mock import Mock

from fuel_engine import ThermoFlowEngine
from fuel_errors import FuelError


class TelemetryAuditAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.start_audit = Mock(return_value={"status": "started"})
        self.status = Mock(return_value={"status": "idle"})
        self.handlers = {
            "stats": Mock(return_value={}),
            "summary": Mock(return_value={}),
            "daily": Mock(return_value={}),
            "telemetry_audit_status": self.status,
            "telemetry_audit_start": self.start_audit,
            "refills": Mock(return_value=[]),
            "add_refill": Mock(return_value={}),
            "void_refill": Mock(return_value={}),
        }

    def engine(self, expected_token="secret"):
        def check_token(headers):
            supplied = headers.get("X-Api-Token", "")
            if supplied != expected_token:
                raise FuelError(401, "Brak lub zły token")

        return ThermoFlowEngine(
            handlers=self.handlers,
            check_token=check_token,
            fuel_error=FuelError,
            warmup=Mock(),
        )

    def test_start_audit_requires_token(self):
        engine = self.engine()
        status, payload = engine.handle(
            "POST", "/api/fuel/telemetry-audit", {}, {}, b""
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": "Brak lub zły token"})
        self.start_audit.assert_not_called()

    def test_start_audit_accepts_valid_token(self):
        engine = self.engine()
        status, payload = engine.handle(
            "POST",
            "/api/fuel/telemetry-audit",
            {},
            {"X-Api-Token": "secret"},
            b"",
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload, {"status": "started"})
        self.start_audit.assert_called_once_with()

    def test_audit_status_remains_read_only_and_public(self):
        engine = self.engine()
        status, payload = engine.handle(
            "GET", "/api/fuel/telemetry-audit", {}, {}, b""
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "idle"})
        self.status.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
