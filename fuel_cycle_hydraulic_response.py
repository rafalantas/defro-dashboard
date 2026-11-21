"""Read-only correlation of ignition-cycle requests with hydraulic telemetry."""
from __future__ import annotations

from datetime import datetime, timedelta

MAX_GAP_SECONDS = 300
BEFORE_MINUTES = 5
AFTER_MINUTES = 15


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _mean(values):
    return round(sum(values) / len(values), 2) if values else None


def build_cycle_hydraulic_response(normalized, ignition_cycles, hydraulic_correlation):
    """Attach nearby pump/valve events and return response to each ignition request.

    This is temporal co-occurrence only. It does not claim that ignition requests
    cause hydraulic changes. Missing samples and gaps remain explicit.
    """
    normalized = normalized or {}
    ignition_cycles = ignition_cycles or []
    hydraulic_correlation = hydraulic_correlation or {}
    returns = sorted(normalized.get("return_c", []), key=lambda item: item[0])
    pump_events = hydraulic_correlation.get("pump_transitions") or []
    valve_events = hydraulic_correlation.get("valve_movements") or []
    results = []

    for cycle in ignition_cycles:
        start = _parse(cycle.get("at"))
        if start is None:
            continue
        before_start = start - timedelta(minutes=BEFORE_MINUTES)
        after_start = start
        after_end = start + timedelta(minutes=AFTER_MINUTES)
        before = [(ts, float(value)) for ts, value in returns
                  if before_start <= ts < after_start]
        after = [(ts, float(value)) for ts, value in returns
                 if after_start <= ts <= after_end]

        # Do not summarize a window as continuous when an observed gap exceeds 5m.
        combined = before + after
        gaps = [(b[0] - a[0]).total_seconds()
                for a, b in zip(combined, combined[1:])]
        coverage_ok = (
            len(before) >= 2 and len(after) >= 2
            and all(0 < gap <= MAX_GAP_SECONDS for gap in gaps)
        )
        event_end = start + timedelta(minutes=AFTER_MINUTES)
        nearby_pumps = [
            event for event in pump_events
            if (event_time := _parse(event.get("at"))) is not None
            and start <= event_time <= event_end
        ]
        nearby_valves = [
            event for event in valve_events
            if (event_time := _parse(event.get("at"))) is not None
            and start <= event_time <= event_end
        ]
        results.append({
            "ignition_request_at": start.isoformat(),
            "return_window": {
                "before_minutes": BEFORE_MINUTES,
                "after_minutes": AFTER_MINUTES,
                "before_samples": len(before),
                "after_samples": len(after),
                "mean_before_c": _mean([value for _, value in before]),
                "mean_after_c": _mean([value for _, value in after]),
                "mean_delta_c": (
                    round(_mean([value for _, value in after]) -
                          _mean([value for _, value in before]), 2)
                    if coverage_ok else None
                ),
                "coverage_status": "observed_continuous_windows" if coverage_ok
                                   else "insufficient_or_gapped",
                "max_gap_seconds": round(max(gaps), 1) if gaps else None,
            },
            "pump_transitions_within_15m": len(nearby_pumps),
            "pump_transitions": nearby_pumps,
            "valve_movements_within_15m": len(nearby_valves),
            "valve_movements": nearby_valves,
            "flame_presence_observed": bool(cycle.get("flame_presence_observed")),
            "temperature_recovery_observed": bool(cycle.get("temperature_recovery_observed")),
            "interpretation": "temporal_cooccurrence_not_causality",
        })

    return {
        "mode": "read_only_ignition_hydraulic_cooccurrence",
        "cycles_analyzed": len(results),
        "cycles_with_return_coverage": sum(
            item["return_window"]["coverage_status"] == "observed_continuous_windows"
            for item in results
        ),
        "cycles_with_pump_events": sum(item["pump_transitions_within_15m"] > 0 for item in results),
        "cycles_with_valve_movements": sum(item["valve_movements_within_15m"] > 0 for item in results),
        "cycles": results[-100:],
        "limitations": [
            "Zdarzenia pomp i zaworu w oknie cyklu oznaczają współwystępowanie, nie przyczynowość.",
            "Okna z luką ponad 5 minut lub mniej niż dwiema próbkami na stronę nie otrzymują delty temperatury.",
            "Brak próbek nie jest interpolowany ani uzupełniany.",
            "Moduł jest wyłącznie diagnostyczny i nie zmienia ochrony powrotu 55°C ani nastaw.",
        ],
        "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
    }
