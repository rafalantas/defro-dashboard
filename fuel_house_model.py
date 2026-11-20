"""House heating model selection from refill periods.

The module is deterministic: data retrieval, cache ownership and the regression
implementation are injected by the compatibility facade.
"""
from __future__ import annotations

from datetime import datetime


def build_training_rows(refills: list[dict], period_temps, *, min_period_days: float,
                        max_days: int) -> list[tuple[float, float, list]]:
    """Build valid refill-period training rows, preserving legacy completeness rules."""
    rows = []
    for previous, current in zip(refills, refills[1:]):
        start = datetime.fromisoformat(previous["ts"])
        end = datetime.fromisoformat(current["ts"])
        days = (end - start).total_seconds() / 86400
        if not min_period_days <= days <= max_days:
            continue
        temperatures, missing = period_temps(start, end)
        if missing or not temperatures:
            continue
        rows.append((days, current["kg"], temperatures))
    return rows


def fit_house_model(rows: list, *, min_model_periods: int, model_bases: list,
                    hdd_base_fixed: float | None, fit_at) -> dict | None:
    """Select the minimum-SSE balance temperature or fit the configured fixed base."""
    if len(rows) < min_model_periods:
        return None
    fits = [fit for fit in (fit_at(rows, base) for base in model_bases) if fit]
    best = min(fits, key=lambda fit: fit["_sse"]) if fits else None
    chosen = fit_at(rows, hdd_base_fixed) if hdd_base_fixed is not None else best
    if not chosen:
        return None
    model = {key: value for key, value in chosen.items() if key != "_sse"}
    model["base_fixed"] = hdd_base_fixed is not None
    model["best_fit_balance_temp"] = best["balance_temp"] if best else None
    model["best_fit_r2"] = best["r2"] if best else None
    return model


def fit_from_refills(refills: list[dict], period_temps, *, min_period_days: float,
                     max_days: int, min_model_periods: int, model_bases: list,
                     hdd_base_fixed: float | None, fit_at) -> dict | None:
    """Build valid period rows and fit the house model."""
    rows = build_training_rows(
        refills, period_temps, min_period_days=min_period_days, max_days=max_days
    )
    return fit_house_model(
        rows, min_model_periods=min_model_periods, model_bases=model_bases,
        hdd_base_fixed=hdd_base_fixed, fit_at=fit_at,
    )


def active_balance_temperature(model: dict | None, *, fixed_base: float | None,
                               fallback_base: float) -> float:
    """Return fixed base, fitted balance temperature, or configured fallback."""
    if fixed_base is not None:
        return fixed_base
    return model["balance_temp"] if model else fallback_base
