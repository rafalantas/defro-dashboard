"""Shared in-memory state for one ThermoFlow engine instance.

This container groups cache objects and synchronization primitives. It does not
own TTL, cache-key, invalidation, persistence, or eviction policy; those remain
owned by the calling facade.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class SingleFlightCache:
    """Ensure only one caller refreshes a cache key at a time.

    The refresh callback must not acquire a cache lock: network I/O belongs
    outside critical sections. Waiters receive the owner's result or error.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._flights = {}

    def run(self, key, refresh):
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                owner = True
            else:
                owner = False

        if owner:
            try:
                flight.result = refresh()
            except BaseException as error:
                flight.error = error
                raise
            finally:
                flight.event.set()
                with self._lock:
                    self._flights.pop(key, None)
            if flight.error is not None:
                raise flight.error
            return flight.result

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        return flight.result


class StageTimer:
    """Thread-safe cumulative timing for named processing stages."""

    def __init__(self):
        self._lock = threading.Lock()
        self.stages = {}

    @contextmanager
    def stage(self, name):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self.stages[name] = self.stages.get(name, 0.0) + elapsed

    def snapshot(self):
        with self._lock:
            return dict(self.stages)


class FuelCacheState:
    """Mutable cache storage with legacy fuel.py shapes and defaults."""

    def __init__(self):
        self.lock = threading.Lock()
        self.daily = {}
        self.openmeteo = {}
        self.temps_start = []
        self.refills = {"at": 0.0, "data": None}
        self.summary = {"at": 0.0, "data": None}
        self.summary_state = {"thread": None, "error": None}
        self.summary_guard = threading.Lock()
        self.offset = {"at": 0.0, "data": None}
        self.forecast = {"at": 0.0, "data": None}
        self.climate = {"at": 0.0, "data": None}
        self.house_model = {"key": None, "data": None}
        self.counter = {}
        self.weather = {}
        self.weather_fetched = {}
        self.daily_model = {"key": None, "data": None}
        self.telemetry_history = {"key": None, "at": 0.0, "data": None}

        self.cache_locks = {
            "daily": threading.Lock(),
            "openmeteo": threading.Lock(),
            "temps_start": threading.Lock(),
            "refills": threading.Lock(),
            "offset": threading.Lock(),
            "forecast": threading.Lock(),
            "climate": threading.Lock(),
            "house_model": threading.Lock(),
            "counter": threading.Lock(),
            "weather": threading.Lock(),
            "daily_model": threading.Lock(),
            "telemetry_history": threading.Lock(),
        }
        self.singleflight = SingleFlightCache()
        self.stage_timer = StageTimer()
