"""Pure burner-energy calibration and conversion functions."""
from __future__ import annotations

from datetime import datetime


def calibrate_burner(usable: list, min_period_days: float) -> dict:
    """Calibrate the burner-energy factor against refill-derived kg."""
    actual = sum(period["kg"] for period in usable)
    estimated = sum(period["est_kg_raw"] for period in usable)
    days = sum(period["days"] for period in usable)
    if not usable or estimated <= 0 or days < min_period_days:
        return {"factor": 1.0, "calibrated": False,
                "periods_used": len(usable), "days": round(days, 1)}
    return {"factor": round(actual / estimated, 3), "calibrated": True,
            "periods_used": len(usable), "days": round(days, 1)}


def calibration(periods: list[dict], counter_epoch: datetime, now: datetime,
                provisional_factor: float, min_period_days: float) -> dict:
    """Build old/new burner calibration while retaining the post-epoch provisional factor."""
    def usable(group):
        return [period for period in group if period["est_kg_raw"] and period["days_missing_burner"] == 0]

    old = calibrate_burner(
        usable([period for period in periods if datetime.fromisoformat(period["end"]) <= counter_epoch]),
        min_period_days,
    )
    new = calibrate_burner(
        usable([period for period in periods if datetime.fromisoformat(period["start"]) >= counter_epoch]),
        min_period_days,
    )
    if not new["calibrated"]:
        new = {**new, "factor": provisional_factor, "provisional": True}
    after = now >= counter_epoch
    current = new if after else old
    return {
        "old": old, "new": new, "after_epoch": after,
        "factor": current["factor"], "calibrated": current["calibrated"],
        "provisional": bool(current.get("provisional")),
        "periods_used": current["periods_used"], "days": current["days"],
    }


def burner_factor(calib: dict, moment: datetime, counter_epoch: datetime) -> float:
    """Return the energy calibration factor applicable at the given timestamp."""
    return (calib["old"] if moment < counter_epoch else calib["new"])["factor"]


def burner_kg(start: datetime, end: datetime, calib: dict, counter_epoch: datetime,
              period_sums, pellet_kwh_per_kg: float, boiler_efficiency: float):
    """Convert burner energy to kg, splitting intervals that cross the feeder epoch."""
    parts = []
    if start < counter_epoch:
        parts.append((start, min(end, counter_epoch)))
    if end > counter_epoch:
        parts.append((max(start, counter_epoch), end))
    if not parts:
        return None
    total = 0.0
    for first, last in parts:
        _hdd, kwh, _hdd_missing, kwh_missing = period_sums(first, last)
        if kwh_missing:
            return None
        total += (kwh / (pellet_kwh_per_kg * boiler_efficiency)
                  * burner_factor(calib, first, counter_epoch))
    return total
