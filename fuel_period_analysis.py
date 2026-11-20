"""Refill-period analysis orchestration for the ThermoFlow fuel engine.

All external evidence (weather/energy aggregates and counter observations) is
injected. The output schema and arithmetic intentionally match the legacy
fuel._periods() contract so this extraction can be parity-tested.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable


def build_periods(
    refills: list[dict],
    *,
    period_sums: Callable,
    counter_drop: Callable,
    model: dict | None,
    active_base: Callable[[], float],
    max_periods: int,
    max_days: int,
    pellet_kwh_per_kg: float,
    boiler_efficiency: float,
    counter_epoch: datetime,
) -> list[dict]:
    """Build the legacy refill-period payload without owning I/O or caches."""
    selected = refills[-(max_periods + 1):]
    out = []
    for prev, cur in zip(selected, selected[1:]):
        start = datetime.fromisoformat(prev["ts"])
        end = datetime.fromisoformat(cur["ts"])
        days = (end - start).total_seconds() / 86400
        if days <= 0 or days > max_days:
            continue

        hdd, kwh, hdd_missing, kwh_missing = period_sums(start, end)
        est_raw = kwh / (pellet_kwh_per_kg * boiler_efficiency) if not kwh_missing else None
        drop, counter_ok, counter_floor = counter_drop(start, end)

        heating = None
        if model and hdd >= 5:
            heating_kg = cur["kg"] - model["base_kg_per_day"] * days
            heating = {
                "kg": round(heating_kg, 1),
                "kg_per_hdd": round(heating_kg / hdd, 2),
                "model_kg": round(
                    model["base_kg_per_day"] * days + model["kg_per_hdd"] * hdd, 1
                ),
            }

        out.append({
            "start": prev["ts"],
            "end": cur["ts"],
            "days": round(days, 2),
            "kg": cur["kg"],
            "kg_per_day": round(cur["kg"] / days, 2),
            "hdd": round(hdd, 1),
            "kg_per_hdd": round(cur["kg"] / hdd, 3) if hdd > 0 else None,
            "useful_kwh_per_hdd": round(
                cur["kg"] * pellet_kwh_per_kg * boiler_efficiency / hdd, 1
            ) if hdd > 0 else None,
            "burner_kwh": round(kwh, 1) if not kwh_missing else None,
            "est_kg_raw": round(est_raw, 1) if est_raw is not None else None,
            "days_missing_temp": hdd_missing,
            "days_missing_burner": kwh_missing,
            "hdd_base": active_base(),
            "heating": heating,
            "counter_drop_percent": round(drop, 1) if counter_ok else None,
            "kg_per_counter_percent": (
                round(cur["kg"] / drop, 2)
                if counter_ok and drop and not counter_floor else None
            ),
            "counter_regime": (
                "old" if end <= counter_epoch
                else "new" if start >= counter_epoch
                else "epoch"
            ),
        })
    return out
