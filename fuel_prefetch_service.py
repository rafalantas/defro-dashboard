"""Parallel telemetry/counter prefetch orchestration."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta


def prefetch(deps: dict, days: int = 400) -> None:
    if not deps["configured"]():
        return

    ts_start = deps["temps_data_start"]()
    today = deps["now"]().date()
    first = max(
        ts_start or today - timedelta(days=deps["max_days"]),
        today - timedelta(days=days),
    )
    wanted = [first + timedelta(days=i) for i in range((today - first).days + 1)]

    def one(day):
        for fetch in (deps["get_daily_raw"], deps["counter_points"]):
            try:
                fetch(day)
            except deps["fuel_error"]:
                # A missing day/source must not stop warming other days.
                pass

    with deps["executor"](max_workers=4) as pool:
        list(pool.map(one, wanted))
