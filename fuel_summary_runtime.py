"""Background summary worker lifecycle and stale-while-refresh behavior."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_logger = logging.getLogger(__name__)
_build_profile = ContextVar("fuel_summary_build_profile", default=None)
_stages = ("temperature_offset", "calibration", "telemetry_fetch")


@contextmanager
def summary_build_profile():
    """Emit one profile per build, even when the build raises."""
    profile = ({name: [0.0, 0] for name in _stages}, [])
    token = _build_profile.set(profile)
    started = perf_counter()
    succeeded = False
    try:
        yield
        succeeded = True
    finally:
        total = perf_counter() - started
        _build_profile.reset(token)
        timings, _ = profile
        parts = " ".join(f"{name}={seconds:.3f}s/{calls}" for name, (seconds, calls) in timings.items())
        other = max(0.0, total - sum(value[0] for value in timings.values()))
        _logger.warning("Fuel summary build profile status=%s total=%.3fs %s other=%.3fs",
                        "ok" if succeeded else "failed", total, parts, other)


@contextmanager
def summary_build_stage(name):
    """Attribute nested work only to its innermost stage."""
    profile = _build_profile.get()
    if profile is None:
        yield
        return
    timings, stack = profile
    frame = [perf_counter(), 0.0]
    stack.append(frame)
    try:
        yield
    finally:
        elapsed = perf_counter() - frame[0]
        stack.pop()
        if stack:
            stack[-1][1] += elapsed
        timings[name][0] += max(0.0, elapsed - frame[1])
        timings[name][1] += 1


def _run_stage(name, operation):
    started = perf_counter()
    try:
        return operation()
    except Exception as error:
        # Exception messages can include upstream request details. Log only the type.
        _logger.warning("Fuel summary %s failed (%s)", name, type(error).__name__)
        raise
    finally:
        elapsed = perf_counter() - started
        if elapsed >= 5:
            _logger.warning("Fuel summary %s took %.1fs", name, elapsed)


def summary_job(deps: dict) -> None:
    try:
        _run_stage("prefetch", deps["prefetch"])
        _run_stage("build", deps["build"])
        deps["state"]["error"] = None
    except deps["fuel_error"] as exc:
        deps["state"]["error"] = exc.message
    except Exception as exc:
        deps["state"]["error"] = f"{type(exc).__name__}: {exc}"[:180]


def summary_kick(deps: dict):
    with deps["guard"]:
        thread = deps["state"]["thread"]
        if thread is None or not thread.is_alive():
            thread = deps["thread_factory"](
                target=lambda: summary_job(deps),
                name="fuel-summary",
                daemon=True,
            )
            deps["state"]["thread"] = thread
            thread.start()
        return thread


def warmup(deps: dict) -> None:
    if deps["configured"]():
        summary_kick(deps)


def get_summary(deps: dict) -> dict:
    if not deps["configured"]():
        return {"configured": False}

    cache = deps["cache"]
    cached = cache["data"]
    if cached is not None:
        if deps["monotonic"]() - cache["at"] >= deps["cache_seconds"]:
            summary_kick(deps)
        return cached

    thread = summary_kick(deps)
    thread.join(timeout=deps["wait_seconds"])
    cached = cache["data"]
    if cached is not None:
        return cached
    if not thread.is_alive() and deps["state"]["error"]:
        raise deps["fuel_error"](502, deps["state"]["error"])
    return {"configured": True, "warming": True}
