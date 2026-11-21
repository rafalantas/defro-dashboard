"""Concurrency tests for ThermoFlow 2.0 cache primitives."""
from __future__ import annotations

import threading
import time

import pytest

from fuel_runtime import FuelCacheState, SingleFlightCache, StageTimer


def test_single_flight_runs_refresh_once():
    cache = SingleFlightCache()
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def refresh():
        with lock:
            calls.append(1)
        time.sleep(0.05)
        return "value"

    results = [None] * 8

    def worker(index):
        barrier.wait()
        results[index] = cache.run("offset", refresh)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert results == ["value"] * 8


def test_single_flight_propagates_error_to_waiters():
    cache = SingleFlightCache()
    barrier = threading.Barrier(4)

    def refresh():
        barrier.wait()
        raise RuntimeError("upstream unavailable")

    errors = []

    def worker():
        barrier.wait()
        try:
            cache.run("forecast", refresh)
        except RuntimeError as error:
            errors.append(str(error))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(errors) == 4
    assert all(error == "upstream unavailable" for error in errors)


def test_cache_locks_are_independent():
    state = FuelCacheState()

    with state.cache_locks["daily"]:
        acquired = state.cache_locks["weather"].acquire(timeout=0.1)
        if acquired:
            state.cache_locks["weather"].release()

    assert acquired is True


def test_stage_timer_is_thread_safe():
    timer = StageTimer()
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        with timer.stage("http"):
            time.sleep(0.01)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert timer.snapshot()["http"] >= 0.01


def test_legacy_lock_and_cache_fields_remain_available():
    state = FuelCacheState()

    assert isinstance(state.lock, type(threading.Lock()))
    assert state.daily == {}
    assert state.summary == {"at": 0.0, "data": None}
    assert state.telemetry_history == {"key": None, "at": 0.0, "data": None}
