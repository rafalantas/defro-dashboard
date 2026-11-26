"""Fail-closed quality gates for persisted Fibaro HC3 event history.

HC3 history is event-driven and sparse: this module never interprets quiet
intervals as missing telemetry and never reconstructs state durations.
"""
from __future__ import annotations

from math import isfinite
from thermoflow_fibaro_event_history import analyze_fibaro_event_history, MAX_HISTORY_SECONDS

SCHEMA = "thermoflow-fibaro-data-quality/1"
ALLOWED_TYPES = {"DevicePropertyUpdatedEvent", "ClimateZoneTemperatureChangedEvent"}


def _issue(code, severity, detail):
    return {"code": code, "severity": severity, "detail": detail}


def _finite_number(value):
    return type(value) in (int, float) and isfinite(value)


def assess_fibaro_history_quality(events, snapshot, *, start, end,
                                  minimum_events=50, minimum_observed_days=7):
    """Assess event integrity and evidence volume; fail closed for ML eligibility."""
    if (type(start) is not int or type(end) is not int or start <= 0
            or end <= start or end - start > 90 * 86400):
        raise ValueError("quality window must be positive and no longer than 90 days")
    if type(minimum_events) is not int or minimum_events < 1:
        raise ValueError("minimum_events must be positive")
    if type(minimum_observed_days) is not int or minimum_observed_days < 1:
        raise ValueError("minimum_observed_days must be positive")
    if not isinstance(events, list) or not isinstance(snapshot, dict):
        raise ValueError("events and snapshot are required")

    issues = []
    seen, timestamps, valid, malformed, duplicates = set(), [], [], 0, 0
    unknown_types = 0
    temperature_values = []
    for event in events:
        if not isinstance(event, dict):
            malformed += 1
            continue
        event_id, stamp = event.get("id"), event.get("timestamp")
        if type(event_id) is not int or event_id <= 0 or type(stamp) is not int or stamp <= 0:
            malformed += 1
            continue
        if not start <= stamp <= end:
            malformed += 1
            continue
        if event_id in seen:
            duplicates += 1
            continue
        seen.add(event_id)
        if event.get("type") not in ALLOWED_TYPES:
            unknown_types += 1
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            malformed += 1
            continue
        if event.get("type") == "ClimateZoneTemperatureChangedEvent":
            old, new = data.get("oldValue"), data.get("newValue")
            if not (_finite_number(old) and _finite_number(new)):
                malformed += 1
                continue
            temperature_values.extend((float(old), float(new)))
        valid.append(event)
        timestamps.append(stamp)

    if malformed:
        issues.append(_issue("malformed_or_out_of_window_events", "blocking",
                             f"{malformed} records failed event schema or window validation."))
    if duplicates:
        issues.append(_issue("duplicate_event_ids", "blocking",
                             f"{duplicates} duplicate HC3 event IDs were found."))
    if temperature_values:
        implausible = sum(value < -10 or value > 45 for value in temperature_values)
        if implausible:
            issues.append(_issue("implausible_temperature_values", "blocking",
                                 f"{implausible} temperature values fall outside -10..45 °C."))

    zones = snapshot.get("zones")
    heads = snapshot.get("heads")
    if not isinstance(zones, list) or not zones or not isinstance(heads, list) or not heads:
        issues.append(_issue("snapshot_mapping_unavailable", "blocking",
                             "A non-empty current zone/head snapshot is required for event mapping."))

    recognized = transitions = suspected_spikes = 0
    recognized_timestamps = set()
    try:
        # The on-demand HC3 analyzer accepts at most seven days per call.
        # Analyze adjacent, non-overlapping inclusive chunks for longer retained
        # datasets; no event is dropped or counted twice at a chunk boundary.
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + MAX_HISTORY_SECONDS, end)
            chunk = [event for event in valid
                     if cursor <= event["timestamp"] <= chunk_end]
            history = analyze_fibaro_event_history(snapshot, chunk, cursor, chunk_end)
            recognized += history["counts"]["events_recognized"]
            transitions += history["counts"]["transitions"]
            suspected_spikes += history["temperature_quality"]["suspected_transient_spikes"]
            for zone in history.get("zones", []):
                if not isinstance(zone, dict):
                    continue
                for transition in zone.get("transitions", []):
                    stamp = transition.get("timestamp") if isinstance(transition, dict) else None
                    if type(stamp) is int and start <= stamp <= end:
                        recognized_timestamps.add(stamp)
            cursor = chunk_end + 1
    except (KeyError, TypeError, ValueError):
        recognized = transitions = suspected_spikes = 0
        issues.append(_issue("history_analysis_failed", "blocking",
                             "Existing event-history analyzer rejected the supplied evidence."))

    observed_days = len({stamp // 86400 for stamp in recognized_timestamps})
    if recognized < minimum_events:
        issues.append(_issue("insufficient_event_volume", "warning",
                             f"{recognized} recognized events; {minimum_events} required."))
    if observed_days < minimum_observed_days:
        issues.append(_issue("insufficient_observed_days", "warning",
                             f"{observed_days} days with recognized transitions; "
                             f"{minimum_observed_days} required."))
    if recognized == 0:
        issues.append(_issue("no_recognized_transitions", "warning",
                             "No supported mapped transitions were observed in this window."))
    blocking = any(item["severity"] == "blocking" for item in issues)
    warning = any(item["severity"] == "warning" for item in issues)
    status = "blocked" if blocking else ("collecting" if warning else "ready")
    return {
        "schema": SCHEMA,
        "status": status,
        "mode": "read_only_shadow",
        "window": {"start": start, "end": end, "duration_seconds": end - start},
        "counts": {"received": len(events), "valid_unique": len(valid),
                   "malformed": malformed, "duplicates": duplicates,
                   "unknown_event_types": unknown_types, "recognized_events": recognized,
                   "transitions": transitions, "observed_utc_days": observed_days,
                   "suspected_temperature_spikes": suspected_spikes},
        "coverage": {
            "semantics": "sparse_event_stream",
            "quiet_intervals_are_gaps": False,
            "state_durations_reconstructed": False,
            "timeline_completeness_claimed": False,
        },
        "training_gate": {
            "eligible": status == "ready",
            "minimum_events": minimum_events,
            "minimum_observed_days": minimum_observed_days,
            "promotion_enabled": False,
        },
        "issues": issues,
        "safety": {"read_only": True, "device_commands_sent": False,
                   "fuel_forecast_affected": False},
    }
