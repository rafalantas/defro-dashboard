"""Conservative read-only analysis of timestamped boiler telemetry.

Episode boundaries are screening heuristics, not proof of burner ignition.
No diagnostic output is connected to boiler controls or fuel estimates.
"""
from __future__ import annotations

MAX_GAP_SECONDS = 300
RETURN_WARNING_C = 54.0
FLUE_ACTIVE_C = 70.0
FLUE_COOL_C = 50.0

def _stats(values):
    values = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {"count": len(values), "min": round(values[0], 2), "median": round(median, 2), "max": round(values[-1], 2)}

def build_raw_diagnostics(points, now):
    """Summarize raw timestamped samples, elevated flue episodes and hydraulics."""
    normalized = {}
    for key, samples in points.items():
        normalized[key] = sorted((ts, float(value)) for ts, value in samples if ts is not None and isinstance(value, (int, float)))
    quality = {}
    for key, samples in normalized.items():
        gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(samples, samples[1:])]
        quality[key] = {"samples": len(samples), "first": samples[0][0].isoformat() if samples else None,
                        "last": samples[-1][0].isoformat() if samples else None,
                        "gaps_over_5m": sum(g > MAX_GAP_SECONDS for g in gaps),
                        "max_gap_seconds": round(max(gaps), 1) if gaps else None}
    flue = normalized.get("flue_c", [])
    power = normalized.get("boiler_power_kw", [])
    episodes, current = [], []
    for ts, temp in flue:
        if current and (ts - current[-1][0]).total_seconds() > MAX_GAP_SECONDS:
            episodes.append(current); current = []
        if temp >= FLUE_ACTIVE_C:
            current.append((ts, temp))
        elif current and temp <= FLUE_COOL_C:
            episodes.append(current); current = []
    if current:
        episodes.append(current)
    firing = []
    for episode in episodes:
        start, end = episode[0][0], episode[-1][0]
        nearby_power = [v for ts, v in power if start <= ts <= end]
        firing.append({"start": start.isoformat(), "end": end.isoformat(),
                       "duration_minutes": round((end - start).total_seconds() / 60, 1),
                       "flue_c": _stats([v for _, v in episode]),
                       "boiler_power_kw": _stats(nearby_power),
                       "classification": "elevated_flue_activity", "ignition_confirmed": False})
    returns = normalized.get("return_c", [])
    low_return = [{"at": ts.isoformat(), "return_c": round(v, 2),
                   "code": "return_below_54c", "severity": "warning"}
                  for ts, v in returns if v < RETURN_WARNING_C]
    hydraulics = {
        "return_c": _stats([v for _, v in returns]),
        "valve_opening_pct": _stats([v for _, v in normalized.get("valve_opening_pct", [])]),
        "boiler_power_kw": _stats([v for _, v in power]),
        "pump_state_samples": {key: sum(v >= 0.5 for _, v in normalized.get(key, []))
                               for key in ("pump_floor", "pump_dhw", "pump_circulation")},
        "low_return_samples": len(low_return), "low_return_events": low_return[:200],
        "reference_return_protection_c": 55.0, "warning_threshold_c": RETURN_WARNING_C,
        "note": "Próbki poniżej 54°C są sygnałem do przeglądu, nie dowodem zadziałania ochrony. Nastawy pozostają bez zmian."}
    return {"mode": "read_only_timestamped_screening", "timezone": str(now.tzinfo),
            "series": quality,
            "firing_activity": {"episodes_count": len(firing), "episodes": firing[-100:],
                "threshold_flue_active_c": FLUE_ACTIVE_C, "threshold_flue_cool_c": FLUE_COOL_C,
                "max_sample_gap_seconds": MAX_GAP_SECONDS,
                "note": "Epizody podwyższonej temperatury spalin; nie są automatycznie utożsamiane z rozpaleniami palnika."},
            "hydraulics": hydraulics,
            "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes"}