"""Build AI observations from real refill-to-refill consumption periods.

A refill closes one observation: kilograms added are the estimated consumption
since the preceding refill. No daily refill or daily hopper-counter sample is
required. Features are normalized by the exact elapsed period duration.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable


def build_refill_period_rows(refills: list[dict], period_features: Callable,
                             *, max_days: int) -> tuple[list[dict], dict]:
    """Build chronological, per-day-rate observations from consecutive refill events.

    period_features(start, end) returns daily slices with fraction (fraction
    of a 24-hour day), hdd_lag, solar_mj and wind_max_kmh. Missing source data
    rejects that interval rather than imputing fabricated values.
    """
    rows = []
    rejected = {"invalid_duration": 0, "period_too_long": 0, "missing_features": 0}
    for previous, current in zip(refills, refills[1:]):
        try:
            start = datetime.fromisoformat(previous["ts"])
            end = datetime.fromisoformat(current["ts"])
            elapsed_days = (end - start).total_seconds() / 86400.0
        except (KeyError, TypeError, ValueError):
            rejected["invalid_duration"] += 1
            continue
        if elapsed_days <= 0:
            rejected["invalid_duration"] += 1
            continue
        if elapsed_days > max_days:
            rejected["period_too_long"] += 1
            continue
        try:
            slices = period_features(start, end)
        except Exception:
            rejected["missing_features"] += 1
            continue
        if not slices:
            rejected["missing_features"] += 1
            continue
        totals = {"hdd_lag": 0.0, "solar_mj": 0.0, "wind_hdd_lag": 0.0}
        covered = 0.0
        valid = True
        for item in slices:
            values = (item.get("fraction"), item.get("hdd_lag"),
                      item.get("solar_mj"), item.get("wind_max_kmh"))
            if any(value is None for value in values):
                valid = False
                break
            try:
                fraction, hdd, solar, wind = map(float, values)
            except (TypeError, ValueError, OverflowError):
                valid = False
                break
            if fraction <= 0 or hdd < 0 or solar < 0 or wind < 0:
                valid = False
                break
            covered += fraction
            totals["hdd_lag"] += hdd * fraction
            totals["solar_mj"] += solar * fraction
            totals["wind_hdd_lag"] += wind * hdd * fraction / 100.0
        if not valid or covered < elapsed_days - (2.0 / 86400.0):
            rejected["missing_features"] += 1
            continue
        try:
            consumed_kg = float(current["kg"])
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["missing_features"] += 1
            continue
        if consumed_kg < 0:
            rejected["missing_features"] += 1
            continue
        average_hdd = totals["hdd_lag"] / elapsed_days
        rows.append({
            "day": end.date().isoformat(),
            "kg_counter": consumed_kg / elapsed_days,
            "hdd_lag": average_hdd,
            "solar_mj": totals["solar_mj"] / elapsed_days,
            "wind_max_kmh": (
                totals["wind_hdd_lag"] / average_hdd
                if average_hdd > 0 else 0.0
            ),
            "wind_hdd_lag": totals["wind_hdd_lag"] / elapsed_days,
            "estimate": False,
            "source": "refill_period",
            "period_days": round(elapsed_days, 4),
            "consumed_kg": round(consumed_kg, 3),
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
    return rows, {
        "source": "refill_periods",
        "periods_available": max(0, len(refills) - 1),
        "periods_accepted": len(rows),
        "periods_rejected": sum(rejected.values()),
        "rejected_by_reason": rejected,
        "daily_refills_required": False,
    }
