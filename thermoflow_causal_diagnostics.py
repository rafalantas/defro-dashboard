"""Conservative read-only interpretation of repeated hydraulic event evidence."""
from __future__ import annotations

from statistics import median


MIN_PAIRED_EVENTS = 3
MEANINGFUL_RETURN_DROP_C = -1.0


def build_hydraulic_hypotheses(hydraulic_diagnostics: dict | None) -> dict:
    """Describe repeated associations without claiming physical causation."""
    hydraulic = hydraulic_diagnostics or {}
    correlations = hydraulic.get("pump_correlations") or {}
    findings = []
    evaluated = {}
    for pump, payload in sorted(correlations.items()):
        events = payload.get("events") or []
        starts = [
            event for event in events
            if event.get("event") == "start"
            and event.get("association_status") == "paired"
            and isinstance(event.get("return_delta_c"), (int, float))
        ]
        deltas = [float(event["return_delta_c"]) for event in starts]
        negative = [value for value in deltas if value < 0]
        record = {
            "paired_start_events": len(deltas),
            "negative_return_delta_events": len(negative),
            "median_return_delta_c": round(median(deltas), 2) if deltas else None,
            "status": "insufficient_evidence",
        }
        if len(deltas) >= MIN_PAIRED_EVENTS:
            if len(negative) >= MIN_PAIRED_EVENTS and median(deltas) <= MEANINGFUL_RETURN_DROP_C:
                record["status"] = "repeated_temporal_association"
                findings.append({
                    "code": "return_temperature_often_falls_after_pump_start",
                    "pump": pump,
                    "severity": "review",
                    "median_return_delta_c": record["median_return_delta_c"],
                    "events": len(deltas),
                    "interpretation": (
                        "Powtarzalny spadek temperatury powrotu po starcie pompy. "
                        "To korelacja czasowa, nie dowód przyczynowości; sprawdź "
                        "równoczesne zmiany odbioru ciepła i pracę pozostałych obiegów."
                    ),
                })
            else:
                record["status"] = "no_repeated_drop_detected"
        evaluated[pump] = record

    return {
        "status": "available" if evaluated else "collecting",
        "evaluated_pumps": evaluated,
        "findings": findings,
        "minimum_paired_start_events": MIN_PAIRED_EVENTS,
        "return_drop_threshold_c": MEANINGFUL_RETURN_DROP_C,
        "method": "median of unique, contiguous, paired return samples after pump starts",
        "causality": "not_established",
        "safety": "read_only; return protection target remains 55 C; no control recommendations",
    }
