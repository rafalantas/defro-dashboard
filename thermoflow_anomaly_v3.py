"""Multi-sensor anomaly screening and evidence-led causal hypotheses.

All outputs are advisory. Temporal association is never promoted to causation.
"""
from math import isfinite
from statistics import median

SCHEMA = "thermoflow-anomaly/3"
MAX_GAP_SECONDS = 75
MAX_AGE_SECONDS = 90
EVENT_WINDOW_SECONDS = 300
MIN_PAIRED_EVENTS = 3
RETURN_DROP_C = -1.0
PUMPS = ("floor", "dhw", "valve4d", "circulation")
TEMPERATURES = ("return_c", "boiler_c", "flue_c")


def _number(value, low=-50.0, high=150.0):
    return (type(value) in (int, float) and isfinite(value)
            and low <= value <= high)


def analyze_multisignal(samples, *, as_of):
    """Find repeated pump/return/valve patterns in synchronized RAM samples."""
    if type(as_of) is not int or as_of <= 0 or not isinstance(samples, (list, tuple)):
        raise ValueError("invalid anomaly input")
    rows = []
    malformed = future = 0
    seen = set()
    duplicate = False
    for item in samples:
        if not isinstance(item, dict) or type(item.get("timestamp")) is not int or item["timestamp"] <= 0:
            malformed += 1
            continue
        ts = item["timestamp"]
        if ts > as_of:
            future += 1
            continue
        if ts in seen:
            duplicate = True
            continue
        seen.add(ts)
        pumps = item.get("pumps") if isinstance(item.get("pumps"), dict) else {}
        normalized_pumps = {}
        for pump in PUMPS:
            value = pumps.get(pump)
            normalized_pumps[pump] = value if type(value) is bool else (
                bool(value) if type(value) in (int, float) and value in (0, 1) else None
            )
        values = {}
        for key in TEMPERATURES:
            value = item.get(key)
            values[key] = float(value) if _number(value) else None
        opening = item.get("valve_opening_pct")
        values["valve_opening_pct"] = float(opening) if _number(opening, 0, 100) else None
        rows.append({"timestamp": ts, "pumps": normalized_pumps, **values})
    rows.sort(key=lambda row: row["timestamp"])
    latest = rows[-1]["timestamp"] if rows else None
    age = as_of - latest if latest is not None else None
    coverage = {
        "samples": len(rows), "malformed_samples": malformed, "future_samples": future,
        "duplicate_timestamps": int(duplicate), "latest_timestamp": latest,
        "age_seconds": age, "max_gap_seconds": None,
    }
    base = {
        "schema": SCHEMA, "version": "3.0", "status": "collecting",
        "data_state": "missing" if not rows else "insufficient_history",
        "coverage": coverage, "patterns": [], "evaluated_pumps": {},
        "minimum_paired_events": MIN_PAIRED_EVENTS,
        "method": "synchronized pump transitions with unique contiguous before/after sensor samples",
        "causality": "not_established",
        "safety": "read_only_advisory; no boiler, pump, valve, setpoint or fuel-control changes; return protection reference remains 55 C",
    }
    if not rows:
        return base
    if duplicate:
        return {**base, "data_state": "ambiguous_timestamps"}
    if age is None or age < 0 or age > MAX_AGE_SECONDS:
        return {**base, "status": "stale", "data_state": "stale"}
    gaps = [b["timestamp"] - a["timestamp"] for a, b in zip(rows, rows[1:])]
    coverage["max_gap_seconds"] = max(gaps) if gaps else None
    if len(rows) < 2 or any(gap > MAX_GAP_SECONDS for gap in gaps):
        return {**base, "data_state": "discontinuous_history"}
    base["data_state"] = "usable"

    events_by_pump = {pump: [] for pump in PUMPS}
    for previous, current in zip(rows, rows[1:]):
        changed = [pump for pump in PUMPS
                   if previous["pumps"][pump] is not None
                   and current["pumps"][pump] is not None
                   and previous["pumps"][pump] != current["pumps"][pump]]
        if not changed:
            continue
        for pump in changed:
            if not current["pumps"][pump]:
                continue
            before = previous
            after = current
            delta = (after["return_c"] - before["return_c"]
                     if before["return_c"] is not None and after["return_c"] is not None else None)
            valve_delta = (after["valve_opening_pct"] - before["valve_opening_pct"]
                           if before["valve_opening_pct"] is not None
                           and after["valve_opening_pct"] is not None else None)
            events_by_pump[pump].append({
                "timestamp": current["timestamp"], "return_delta_c": round(delta, 2) if delta is not None else None,
                "valve_opening_delta_pct": round(valve_delta, 2) if valve_delta is not None else None,
                "boiler_delta_c": round(after["boiler_c"] - before["boiler_c"], 2)
                    if before["boiler_c"] is not None and after["boiler_c"] is not None else None,
                "flue_delta_c": round(after["flue_c"] - before["flue_c"], 2)
                    if before["flue_c"] is not None and after["flue_c"] is not None else None,
                "other_pumps_changed": [other for other in changed if other != pump],
                "window_seconds": current["timestamp"] - previous["timestamp"],
            })

    patterns = []
    evaluated = {}
    for pump, events in events_by_pump.items():
        paired = [event for event in events if event["return_delta_c"] is not None]
        drops = [event for event in paired if event["return_delta_c"] <= RETURN_DROP_C]
        values = [event["return_delta_c"] for event in paired]
        median_delta = round(median(values), 2) if values else None
        record = {
            "pump": pump, "start_events": len(events), "paired_events": len(paired),
            "return_drop_events": len(drops), "median_return_delta_c": median_delta,
            "status": "insufficient_evidence",
        }
        if len(paired) >= MIN_PAIRED_EVENTS:
            record["status"] = "repeated_return_drop_pattern" if (
                len(drops) >= MIN_PAIRED_EVENTS and median_delta <= RETURN_DROP_C
            ) else "no_repeated_drop_detected"
        evaluated[pump] = record
        if record["status"] == "repeated_return_drop_pattern":
            concurrent = sum(bool(event["other_pumps_changed"]) for event in drops)
            valve_values = [event["valve_opening_delta_pct"] for event in drops
                            if event["valve_opening_delta_pct"] is not None]
            patterns.append({
                "code": "repeated_return_drop_after_pump_start",
                "pump": pump, "severity": "review",
                "paired_events": len(paired), "return_drop_events": len(drops),
                "median_return_delta_c": median_delta,
                "median_valve_opening_delta_pct": round(median(valve_values), 2) if valve_values else None,
                "events_with_concurrent_pump_changes": concurrent,
                "confidence": "low" if concurrent else "moderate",
                "interpretation": (
                    "Powtarzalny spadek temperatury powrotu w próbkach bezpośrednio po starcie pompy. "
                    "To zależność czasowa; równoczesne zmiany obiegów mogą być czynnikiem zakłócającym."
                ),
            })
    return {
        **base, "status": "available", "evaluated_pumps": evaluated, "patterns": patterns,
        "causal_diagnostics": {
            "status": "available" if evaluated else "collecting",
            "findings": [{
                "hypothesis": "start pompy może współwystępować ze spadkiem temperatury powrotu",
                "evidence_code": pattern["code"], "pump": pattern["pump"],
                "confidence": pattern["confidence"], "paired_events": pattern["paired_events"],
                "median_return_delta_c": pattern["median_return_delta_c"],
                "confounders": (["równoczesna zmiana stanu innej pompy"]
                                if pattern["events_with_concurrent_pump_changes"] else []),
                "limitations": [
                    "obserwacja czasowa nie dowodzi przyczynowości",
                    "próbki przed i po zdarzeniu są sąsiednimi odczytami, nie eksperymentem kontrolowanym",
                    "brak pomiaru przepływu i pełnego stanu wszystkich odbiorników",
                ],
            } for pattern in patterns],
            "causality": "not_established",
        },
    }
