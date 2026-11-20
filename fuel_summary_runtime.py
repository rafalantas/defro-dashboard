"""Background summary worker lifecycle and stale-while-refresh behavior."""
from __future__ import annotations


def summary_job(deps: dict) -> None:
    try:
        deps["prefetch"]()
        deps["build"]()
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
