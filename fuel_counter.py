"""Pure feeder-counter calibration and epoch-aware consumption calculations."""
from __future__ import annotations

from datetime import date, datetime, timedelta


def calibrate_pairs(pairs: list, counter_drop, min_counter_drop: float) -> dict:
    """Fit kg per counter percentage point from complete refill intervals."""
    kg = drop_total = 0.0
    used = 0
    for previous, current in pairs:
        start = datetime.fromisoformat(previous["ts"])
        end = datetime.fromisoformat(current["ts"])
        drop, complete, floor_hit = counter_drop(start, end)
        if not complete or floor_hit or not drop:
            continue
        kg += current["kg"]
        drop_total += drop
        used += 1
    if used == 0 or drop_total < min_counter_drop:
        return {"kg_per_percent": None, "calibrated": False,
                "periods_used": used, "drop_percent": round(drop_total, 1)}
    return {"kg_per_percent": round(kg / drop_total, 3), "calibrated": True,
            "periods_used": used, "drop_percent": round(drop_total, 1), "kg": round(kg, 1)}


def span_estimate(refills: list[dict], old: dict, counter_epoch: datetime,
                  counter_drop, min_span_drop: float, uncertainty: float):
    """Estimate the post-epoch coefficient from the refill interval crossing the epoch."""
    if not old["calibrated"]:
        return None
    for previous, current in zip(refills, refills[1:]):
        start = datetime.fromisoformat(previous["ts"])
        end = datetime.fromisoformat(current["ts"])
        if not start < counter_epoch < end:
            continue
        before, ok_before, floor_before = counter_drop(start, counter_epoch)
        after, ok_after, floor_after = counter_drop(counter_epoch, end)
        if not (ok_before and ok_after) or floor_before or floor_after:
            return None
        kg_before = before * old["kg_per_percent"]
        kg_after = current["kg"] - kg_before
        if after < min_span_drop or kg_after <= 0:
            return None
        factor = kg_after / after
        return {
            "kg_per_percent": round(factor, 3),
            "uncertainty_percent": round(uncertainty * 100),
            "low": round(factor * (1 - uncertainty), 3),
            "high": round(factor * (1 + uncertainty), 3),
            "drop_before_percent": round(before, 1),
            "drop_after_percent": round(after, 1),
            "kg_before": round(kg_before, 1),
            "kg_after": round(kg_after, 1),
            "refill_ts": current["ts"],
        }
    return None


def _calibration_confidence(periods_used: int, source: str) -> dict:
    """Describe evidence volume; this is not a statistical confidence interval."""
    count = max(0, int(periods_used or 0))
    if source == "wartość tymczasowa":
        return {"level": "none", "label": "brak kalibracji · współczynnik tymczasowy",
                "periods_used": count, "basis": "provisional"}
    if source == "okres obejmujący epokę":
        return {"level": "low", "label": "niska · szacunek z okresu granicznego",
                "periods_used": 1, "basis": "epoch_spanning_estimate"}
    if count <= 1:
        level, label = "low", "niska · pojedynczy okres"
    elif count <= 3:
        level, label = "limited", "ograniczona · 2–3 okresy"
    elif count <= 7:
        level, label = "moderate", "umiarkowana · 4–7 okresów"
    else:
        level, label = "high", "wyższa · co najmniej 8 okresów"
    return {"level": level, "label": label, "periods_used": count,
            "basis": "complete_post_epoch_refill_periods"}


def counter_calibration(refills: list[dict], counter_epoch: datetime, now: datetime,
                        counter_drop, min_counter_drop: float, min_span_drop: float,
                        provisional_factor: float, uncertainty: float,
                        collecting_message: str, format_pl) -> dict:
    """Calibrate old/new feeder regimes and select measured, span-estimated or provisional factor."""
    old_pairs, new_pairs = [], []
    for previous, current in zip(refills, refills[1:]):
        start = datetime.fromisoformat(previous["ts"])
        end = datetime.fromisoformat(current["ts"])
        if end <= counter_epoch:
            old_pairs.append((previous, current))
        elif start >= counter_epoch:
            new_pairs.append((previous, current))
    old = calibrate_pairs(old_pairs, counter_drop, min_counter_drop)
    new = calibrate_pairs(new_pairs, counter_drop, min_counter_drop)
    span = span_estimate(refills, old, counter_epoch, counter_drop, min_span_drop, uncertainty)
    after = now >= counter_epoch
    uncertainty_percent = round(uncertainty * 100)

    if new["calibrated"]:
        effective = {"kg_per_percent": new["kg_per_percent"], "source": "okresy po epoce", "measured": True}
    elif span:
        effective = {"kg_per_percent": span["kg_per_percent"], "source": "okres obejmujący epokę",
                     "measured": True, "uncertainty_percent": span["uncertainty_percent"]}
    else:
        effective = {"kg_per_percent": provisional_factor, "source": "wartość tymczasowa",
                     "measured": False, "uncertainty_percent": uncertainty_percent}
    collecting = after and not new["calibrated"]

    message = None
    if collecting and effective["source"] == "okres obejmujący epokę":
        message = (f"{collecting_message}, szacunek z okresu obejmującego zmianę podajnika: "
                   f"{format_pl(effective['kg_per_percent'])} kg/% (±{uncertainty_percent}%)")
    elif collecting:
        message = (f"{collecting_message}, tymczasowo {format_pl(effective['kg_per_percent'])} "
                   f"kg/% (szacunek, ±{uncertainty_percent}%)")

    current = new if after else old
    confidence = _calibration_confidence(
        new["periods_used"] if after else old["periods_used"],
        effective["source"] if after else ("okresy po epoce" if old["calibrated"] else "wartość tymczasowa"),
    )
    return {
        "epoch": counter_epoch.isoformat(), "after_epoch": after,
        "confidence": confidence,
        "old": old, "new": new, "span": span, "new_effective": effective,
        "collecting": collecting, "provisional": collecting, "message": message,
        "kg_per_percent": effective["kg_per_percent"] if after else old["kg_per_percent"],
        "calibrated": current["calibrated"],
        "periods_used": current["periods_used"], "drop_percent": current["drop_percent"],
    }


def counter_factor(counter: dict, moment: datetime, counter_epoch: datetime):
    """Return the calibrated/provisional kg per percentage point for a timestamp."""
    if moment < counter_epoch:
        group = counter["old"]
        return group["kg_per_percent"] if group["calibrated"] else None
    return counter["new_effective"]["kg_per_percent"]


def counter_kg(start: datetime, end: datetime, counter: dict, counter_epoch: datetime, counter_drop):
    """Calculate kg across one or both feeder regimes; preserve completeness and empty-floor flags."""
    parts = []
    if start < counter_epoch:
        parts.append((start, min(end, counter_epoch)))
    if end > counter_epoch:
        parts.append((max(start, counter_epoch), end))
    if not parts:
        return None, False, False
    total, floor = 0.0, False
    for first, last in parts:
        factor = counter_factor(counter, first, counter_epoch)
        if factor is None:
            return None, False, False
        drop, complete, floor_hit = counter_drop(first, last)
        if not complete or drop is None:
            return None, False, False
        total += drop * factor
        floor = floor or floor_hit
    return total, True, floor


def counter_regime(day: date, day_bounds, counter_epoch: datetime) -> str:
    """Classify a local day as old, new or epoch-transition day."""
    start, end = day_bounds(day)
    if end <= counter_epoch:
        return "old"
    return "new" if start >= counter_epoch else "epoch"


def counter_method(counter: dict, format_pl) -> str:
    """Describe the active coefficient without inventing uncertainty for a calibrated value."""
    effective = counter["new_effective"]
    value = format_pl(effective["kg_per_percent"])
    source = effective["source"]
    if source == "okres obejmujący epokę":
        uncertainty = effective["uncertainty_percent"]
        return (f"licznik podajnika, współczynnik z okresu obejmującego zmianę podajnika "
                f"{value} kg/% (szacunek, ±{uncertainty}%)")
    if source == "wartość tymczasowa":
        uncertainty = effective["uncertainty_percent"]
        return (f"licznik podajnika, współczynnik tymczasowy {value} kg/% "
                f"(szacunek, ±{uncertainty}%)")
    return "licznik podajnika skalibrowany do dosypek"


def movement_diagnostics(sequence: list[tuple[float, float]], tz) -> dict:
    """Summarize signed counter movement and the largest sample gap for one day."""
    if not sequence:
        return {}
    values = [value for _stamp, value in sequence]
    deltas = [cur[1] - prev[1] for prev, cur in zip(sequence, sequence[1:])]
    rises = [delta for delta in deltas if delta > 0]
    drops = [-delta for delta in deltas if delta < 0]
    gaps = [(cur[0] - prev[0], prev[0], cur[0])
            for prev, cur in zip(sequence, sequence[1:])]
    max_gap = max(gaps, default=None, key=lambda item: item[0])
    result = {
        "upward_total_pct": round(sum(rises), 2),
        "downward_total_pct": round(sum(drops), 2),
        "net_change_pct": round(values[-1] - values[0], 2),
        "upward_changes": len(rises),
        "downward_changes": len(drops),
        "min_counter_pct": min(values),
        "max_counter_pct": max(values),
        "largest_rise_pct": round(max(rises), 2) if rises else 0.0,
        "largest_drop_pct": round(max(drops), 2) if drops else 0.0,
        "max_gap_seconds": int(max_gap[0]) if max_gap else None,
        "max_gap_from": datetime.fromtimestamp(max_gap[1], tz).isoformat() if max_gap else None,
        "max_gap_to": datetime.fromtimestamp(max_gap[2], tz).isoformat() if max_gap else None,
    }
    return result
