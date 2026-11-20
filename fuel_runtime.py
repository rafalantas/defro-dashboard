"""Shared in-memory state for one ThermoFlow engine instance.

This container only groups the existing cache objects and synchronization
primitives. It deliberately does not define TTLs, cache keys, invalidation,
persistence, or eviction policy; those remain owned by the current facade.
"""
from __future__ import annotations

import threading


class FuelCacheState:
    """Mutable cache storage with the legacy fuel.py shapes and defaults."""

    def __init__(self):
        self.lock = threading.Lock()
        self.daily: dict[str, dict] = {}
        self.openmeteo: dict[str, float] = {}
        self.temps_start: list = []
        self.refills: dict = {"at": 0.0, "data": None}
        self.summary: dict = {"at": 0.0, "data": None}
        self.summary_state: dict = {"thread": None, "error": None}
        self.summary_guard = threading.Lock()
        self.offset: dict = {"at": 0.0, "data": None}
        self.forecast: dict = {"at": 0.0, "data": None}
        self.climate: dict = {"at": 0.0, "data": None}
        self.house_model: dict = {"key": None, "data": None}
        self.counter: dict[str, list] = {}
        self.weather: dict[str, dict] = {}
        self.weather_fetched: dict[str, float] = {}
        self.daily_model: dict = {"key": None, "data": None}
        self.telemetry_history: dict = {"key": None, "at": 0.0, "data": None}
