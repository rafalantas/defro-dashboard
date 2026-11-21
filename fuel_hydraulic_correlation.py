"""Read-only temporal correlation of hydraulic telemetry signals.

Uses observed timestamped samples only. It does not interpolate, infer commands,
or recommend/control pump and valve settings.
"""
from __future__ import annotations

from datetime import timedelta

MAX_GAP_SECONDS = 300
EVENT_WINDOW_MINUTES = 5
VALVE_MOVEMENT_THRESHOLD_PCT = 5.0
PUMP_KEYS = ("pump_floor", "pump_dhw", "pump_circulation")


def _window(samples, start, end):
    return [(ts, value) for ts, value in samples if start <= ts <= end]


def _mean(samples):
    if not samples:
        return None
    return round(sum(value for _, value in samples) / len(samples), 2)


def _delta(before, after):
    before_mean, after_mean = _mean(before), _mean(after)
    if before_mean is None or after_mean is None:
        return None
    return round(after_mean - before_mean, 2)


def _transition_events(samples, key, label):
    events = []
    for (previous_ts, previous), (ts, value) in zip(samples, samples[1:]):
        gap = (ts - previous_ts).total_seconds()
        if 0 < gap <= MAX_GAP_SECONDS and (previous >= 0.5) != (value >= 0.5):
            events.append({
                "signal": key,
                "label": label,
                "at": ts.isoformat(),
                "transition": "on" if value >= 0.5 else "off",
                "gap_seconds": round(gap, 1),
            })
    return events


def _response(event_time, returns, valve):
    window = timedelta(minutes=EVENT_WINDOW_MINUTES)
    before_return = _window(returns, event_time - window, event_time)
    after_return = _window(returns, event_time, event_time + window)
    before_valve = _window(valve, event_time - window, event_time)
    after_valve = _window(valve, event_time, event_time + window)
    return {
        "window_minutes_each_side": EVENT_WINDOW_MINUTES,
        "return_before_samples": len(before_return),
        "return_after_samples": len(after_return),
        "return_mean_before_c": _mean(before_return),
        "return_mean_after_c": _mean(after_return),
        "return_mean_delta_c": _delta(before_return, after_return),
        "valve_before_samples": len(before_valve),
        "valve_after_samples": len(after_valve),
        "valve_mean_before_pct": _mean(before_valve),
        "valve_mean_after_pct": _mean(after_valve),
        "valve_mean_delta_pct": _delta(before_valve, after_valve),
        "response_status": (
            "observed_both_windows"
            if len(before_return) >= 2 and len(after_return) >= 2
            else "insufficient_window_coverage"
        ),
    }


def build_hydraulic_correlation(normalized):
    """Summarize observed pump/valve transitions and bounded return response."""
    normalized = normalized or {}
    returns = normalized.get("return_c", [])
    valve = normalized.get("valve_opening_pct", [])
    labels = {
        "pump_floor": "Pompa podłogowa",
        "pump_dhw": "Pompa CWU",
        "pump_circulation": "Pompa cyrkulacyjna",
    }
    pump_events = []
    for key in PUMP_KEYS:
        for event in _transition_events(normalized.get(key, []), key, labels[key]):
            at = next(ts for ts, _ in normalized[key] if ts.isoformat() == event["at"])
            event["return_response"] = _response(at, returns, valve)
            pump_events.append(event)

    valve_events = []
    for (previous_ts, previous), (ts, value) in zip(valve, valve[1:]):
        gap = (ts - previous_ts).total_seconds()
        change = value - previous
        if 0 < gap <= MAX_GAP_SECONDS and abs(change) >= VALVE_MOVEMENT_THRESHOLD_PCT:
            event = {
                "signal": "valve_opening_pct",
                "label": "Ruch zaworu 4D",
                "at": ts.isoformat(),
                "change_pct": round(change, 2),
                "from_pct": round(previous, 2),
                "to_pct": round(value, 2),
                "gap_seconds": round(gap, 1),
            }
            event["return_response"] = _response(ts, returns, valve)
            valve_events.append(event)

    return {
        "mode": "read_only_timestamped_hydraulic_correlation",
        "max_sample_gap_seconds": MAX_GAP_SECONDS,
        "event_window_minutes_each_side": EVENT_WINDOW_MINUTES,
        "valve_movement_threshold_pct": VALVE_MOVEMENT_THRESHOLD_PCT,
        "pump_transitions_count": len(pump_events),
        "pump_transitions": pump_events[-100:],
        "valve_movements_count": len(valve_events),
        "valve_movements": valve_events[-100:],
        "limitations": [
            "Porównanie opisuje współwystępowanie próbek, nie dowodzi związku przyczynowego.",
            "Okna z mniej niż dwiema próbkami temperatury po którejkolwiek stronie są oznaczane jako niewystarczające.",
            "Przerwy dłuższe niż 5 minut nie są łączone ani interpolowane.",
            "Pompa zaworu 4D nie jest analizowana jako przełączana; jej praca jest stała.",
            "Ochrona powrotu 55°C pozostaje niezmieniona; moduł nie steruje instalacją.",
        ],
        "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
    }
