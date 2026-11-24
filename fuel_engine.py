"""Runtime boundary and HTTP dispatcher for the modular ThermoFlow engine.

Importing this module does not load the optional fuel subsystem. Production
route dispatch uses injected service callbacks; LegacyFuelEngine remains a
fallback adapter for third-party integrations and tests.
"""
from __future__ import annotations

import json
from importlib import import_module
from typing import Protocol


class FuelEngine(Protocol):
    """Runtime contract consumed by api.py."""

    def handle(self, method: str, path: str, query: dict, headers, body: bytes):
        """Return the legacy (HTTP status, JSON payload) tuple or None."""
        ...

    def warmup(self) -> None:
        """Start optional background cache warmup."""
        ...


class LegacyFuelEngine:
    """Compatibility adapter around the existing fuel.py implementation."""

    def __init__(self, implementation):
        self._implementation = implementation

    def handle(self, method: str, path: str, query: dict, headers, body: bytes):
        return self._implementation.handle(method, path, query, headers, body)

    def warmup(self) -> None:
        self._implementation.warmup()


class ThermoFlowEngine:
    """Dispatch public fuel routes to injected service workflows."""

    def __init__(self, *, handlers: dict, check_token, fuel_error, warmup):
        self._handlers = handlers
        self._check_token = check_token
        self._fuel_error = fuel_error
        self._warmup = warmup

    def handle(self, method: str, path: str, query: dict, headers, body: bytes):
        if not path.startswith("/api/fuel/"):
            return None
        if path != "/api/fuel/" and path.endswith("/"):
            path = path.rstrip("/")
        try:
            if method == "GET" and path == "/api/fuel/stats":
                return 200, self._handlers["stats"](query)
            if method == "GET" and path == "/api/fuel/summary":
                return 200, self._handlers["summary"]()
            if method == "GET" and path == "/api/fuel/daily":
                return 200, self._handlers["daily"](query)
            if method == "GET" and path == "/api/fuel/telemetry-audit":
                return 200, self._handlers["telemetry_audit_status"]()
            if method == "POST" and path == "/api/fuel/telemetry-audit":
                self._check_token(headers)
                return 202, self._handlers["telemetry_audit_start"]()
            if method == "GET" and path == "/api/fuel/refills":
                return 200, {"refills": self._handlers["refills"]()}
            if method == "POST" and path == "/api/fuel/refill":
                self._check_token(headers)
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise self._fuel_error(400, "Treść musi być poprawnym JSON") from None
                return 201, self._handlers["add_refill"](
                    data if isinstance(data, dict) else {}
                )
            if method == "DELETE" and path.startswith("/api/fuel/refill/"):
                self._check_token(headers)
                return 200, self._handlers["void_refill"](path.rsplit("/", 1)[1])
            if method == "GET" and path == "/api/fuel/refill":
                return 405, {"error": "Dosypki dodaje się metodą POST; listę zwraca /api/fuel/refills"}
            return 404, {"error": "Nie znaleziono"}
        except self._fuel_error as error:
            return error.status, {"error": error.message}

    def warmup(self) -> None:
        self._warmup()


def load(importer=None) -> FuelEngine:
    """Load the current engine only when the API explicitly requests it.

    The importer argument exists for deterministic boundary tests; production
    defaults to Python's regular module loader.
    """
    loader = importer or import_module
    implementation = loader("fuel")
    factory = getattr(implementation, "create_engine", None)
    if callable(factory):
        return factory()
    return LegacyFuelEngine(implementation)
