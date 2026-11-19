"""Stable ThermoFlow engine boundary.

The current implementation is still the legacy fuel module. This adapter
makes the runtime contract explicit so a shadow-tested ThermoFlow 2 engine
can replace it without changing API route dispatch or warmup semantics.
Importing this module does not import the optional legacy implementation.
"""
from __future__ import annotations

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


def load(importer=None) -> FuelEngine:
    """Load the current engine only when the API explicitly requests it.

    The importer argument exists for deterministic boundary tests; production
    defaults to Python's regular module loader.
    """
    loader = importer or import_module
    implementation = loader("fuel")
    return LegacyFuelEngine(implementation)
