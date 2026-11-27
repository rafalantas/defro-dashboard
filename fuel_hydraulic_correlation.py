"""Read-only temporal correlation of hydraulic telemetry signals.

Uses observed timestamped samples only. It does not interpolate, infer commands,
or recommend/control pump and valve settings.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from datetime import timedelta
from operator import itemgetter

MAX_GAP_SECONDS = 300
EVENT_WINDOW_MINUTES = 5
VALVE_MOVEMENT_THRESHOLD_PCT = 5.0
PUMP_KEYS = ("pump_floor", "pump_dhw", "pump_circulation")
REPORTED_EVENTS = 100

_BY_TIME = itemgetter(0)


def window_samples(samples, start, end, include_end=True):
    """Samples with ``start <= ts <= end`` (``ts < end`` when ``include_end`` is false), in order.

    ``samples`` must be sorted by timestamp, as ``fuel_telemetry_diagnostics._normalize`` returns them.
    The window is then a slice found by binary search; filtering the whole series for every event made
    the telemetry audit cost grow with events x samples (more than half of a cold fuel summary).
    """
    low = bisect_left(samples, start, key=_BY_TIME)
    high = (bisect_right if include_end else bisect_left)(samples, end, key=_BY_TIME)
    return samples[low:high]


_window = window_samples


def _sorted_by_time(samples):
    """The same list when already ordered (the normal case), otherwise a stable sorted copy."""
    if all(a[0] <= b[0] for a, b in zip(samples, samples[1:])):
        return samples
    return sorted(samples, key=_BY_TIME)


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
    for (previous_ts, previous), (ts, value) in zip(samples, samples[1:]):
        gap = (ts - previous_ts).total_seconds()
        if 0 < gap <= MAX_GAP_SECONDS and (previous >= 0.5) != (value >= 0.5):
            yield ts, {
                "signal": key,
                "label": label,
                "at": ts.isoformat(),
                "transition": "on" if value >= 0.5 else "off",
                "gap_seconds": round(gap, 1),
            }


def _response(event_time, returns, valve):
    window = timedelta(minutes=EVENT_WINDOW_MINUTES)
    # Keep the event timestamp in the post-event window only. Including it
    # on both sides biases the delta and can make a one-sample side look valid.
    before_return = window_samples(returns, event_time - window, event_time, include_end=False)
    after_return = _window(returns, event_time, event_time + window)
    before_valve = window_samples(valve, event_time - window, event_time, include_end=False)
    after_valve = _window(valve, event_time, event_time + window)
    return_covered = len(before_return) >= 2 and len(after_return) >= 2
    valve_covered = len(before_valve) >= 2 and len(after_valve) >= 2
    return {
        "window_minutes_each_side": EVENT_WINDOW_MINUTES,
        "return_before_samples": len(before_return),
        "return_after_samples": len(after_return),
        "return_mean_before_c": _mean(before_return),
        "return_mean_after_c": _mean(after_return),
        "return_mean_delta_c": _delta(before_return, after_return) if return_covered else None,
        "valve_before_samples": len(before_valve),
        "valve_after_samples": len(after_valve),
        "valve_mean_before_pct": _mean(before_valve),
        "valve_mean_after_pct": _mean(after_valve),
        "valve_mean_delta_pct": _delta(before_valve, after_valve) if valve_covered else None,
        "response_status": (
            "observed_both_windows" if return_covered
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
    # The payload and downstream cycle diagnostics expose only these events.
    # Count all transitions, but do not scan history for discarded responses.
    pump_events = deque(maxlen=REPORTED_EVENTS)
    pump_count = 0
    for key in PUMP_KEYS:
        for item in _transition_events(normalized.get(key, []), key, labels[key]):
            pump_count += 1
            pump_events.append(item)

    valve_events = deque(maxlen=REPORTED_EVENTS)
    valve_count = 0
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
            valve_count += 1
            valve_events.append((ts, event))

    # Window lookups need ordered series; the event scans above keep using the input as given.
    returns_by_time, valve_by_time = _sorted_by_time(returns), _sorted_by_time(valve)

    def with_responses(events):
        return [dict(event, return_response=_response(ts, returns_by_time, valve_by_time))
                for ts, event in events]

    return {
        "mode": "read_only_timestamped_hydraulic_correlation",
        "max_sample_gap_seconds": MAX_GAP_SECONDS,
        "event_window_minutes_each_side": EVENT_WINDOW_MINUTES,
        "valve_movement_threshold_pct": VALVE_MOVEMENT_THRESHOLD_PCT,
        "pump_transitions_count": pump_count,
        "pump_transitions": with_responses(pump_events),
        "valve_movements_count": valve_count,
        "valve_movements": with_responses(valve_events),
        "limitations": [
            "Porównanie opisuje współwystępowanie próbek, nie dowodzi związku przyczynowego.",
            "Okna z mniej niż dwiema próbkami temperatury po którejkolwiek stronie są oznaczane jako niewystarczające.",
            "Przerwy dłuższe niż 5 minut nie są łączone ani interpolowane.",
            "Pompa zaworu 4D nie jest analizowana jako przełączana; jej praca jest stała.",
            "Ochrona powrotu 55°C pozostaje niezmieniona; moduł nie steruje instalacją.",
        ],
        "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
    }
