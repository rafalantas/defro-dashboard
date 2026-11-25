"""On-demand, read-only analysis of Fibaro HC3 event history."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import isfinite

MAX_HISTORY_SECONDS = 7 * 24 * 60 * 60
MAX_TRANSITIONS = 1000
TRACKED_DEVICE_PROPERTIES = {"heatRequired", "windowOpened"}
TEMPERATURE_SPIKE_MIN_DELTA_C = 5.0
TEMPERATURE_SPIKE_RECOVERY_SECONDS = 15 * 60
TEMPERATURE_SPIKE_RECOVERY_TOLERANCE_C = 1.0


def _identifier(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and str(result) == str(value).strip() else None


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return int(value)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _zone_index(snapshot):
    zones = snapshot.get("zones") if isinstance(snapshot, dict) else None
    if not isinstance(zones, list):
        raise ValueError("Snapshot Fibaro HC3 nie zawiera listy stref")
    by_head, by_zone = {}, {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = _identifier(zone.get("id"))
        if zone_id is None:
            continue
        by_zone[zone_id] = zone
        properties = zone.get("properties")
        devices = properties.get("devices") if isinstance(properties, dict) else []
        if isinstance(devices, list):
            for device_id in devices:
                normalized = _identifier(device_id)
                if normalized is not None:
                    by_head.setdefault(normalized, []).append(zone_id)
    return by_head, by_zone


def _temperature_spike_flags(transitions):
    """Flag a large jump followed quickly by a return near the prior value."""
    temperatures = [item for item in transitions if item.get("kind") == "temperature"]
    flagged = set()
    for current, following in zip(temperatures, temperatures[1:]):
        jump = abs(current["new_value_c"] - current["old_value_c"])
        elapsed = following["timestamp"] - current["timestamp"]
        recovered = abs(following["new_value_c"] - current["old_value_c"])
        continued = abs(following["old_value_c"] - current["new_value_c"])
        if (jump >= TEMPERATURE_SPIKE_MIN_DELTA_C
                and 0 <= elapsed <= TEMPERATURE_SPIKE_RECOVERY_SECONDS
                and recovered <= TEMPERATURE_SPIKE_RECOVERY_TOLERANCE_C
                and continued <= TEMPERATURE_SPIKE_RECOVERY_TOLERANCE_C):
            flagged.update((current["event_id"], following["event_id"]))
    for item in temperatures:
        if item["event_id"] in flagged:
            item["quality"] = "suspected_transient_spike"
            item["quality_reason"] = (
                "Large temperature jump returned near the previous value within 15 minutes; "
                "raw evidence is retained but should not enter clean-temperature features."
            )
        else:
            item["quality"] = "observed"
    return len(flagged)


def _snapshot_heat_required(snapshot):
    heads = snapshot.get("heads") if isinstance(snapshot, dict) else None
    counts = Counter()
    if not isinstance(heads, list):
        return {"available": False, "true": 0, "false": 0, "unknown": 0}
    for head in heads:
        props = head.get("properties") if isinstance(head, dict) else None
        value = props.get("heatRequired") if isinstance(props, dict) else None
        if value is True:
            counts["true"] += 1
        elif value is False:
            counts["false"] += 1
        else:
            counts["unknown"] += 1
    return {"available": True, "true": counts["true"], "false": counts["false"], "unknown": counts["unknown"]}


def analyze_fibaro_event_history(snapshot: dict, events: list, start: int, end: int) -> dict:
    """Summarize observed transitions and expose why tracked events are absent."""
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int)
            or start >= end or end - start > MAX_HISTORY_SECONDS):
        raise ValueError("Zakres historii Fibaro HC3 musi wynosić od 1 sekundy do 7 dni")
    if not isinstance(events, list):
        raise ValueError("Historia zdarzeń Fibaro HC3 musi być listą")

    by_head, by_zone = _zone_index(snapshot)
    zone_transitions = {zone_id: [] for zone_id in by_zone}
    recognized = ignored = mapped_head_device_events = 0
    valid_timestamps = set()
    event_type_counts = Counter()
    mapped_head_property_counts = Counter()
    tracked_property_events = Counter()
    tracked_property_valid_transitions = Counter()

    for event in events:
        if not isinstance(event, dict):
            ignored += 1
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_type_counts[event_type] += 1
        timestamp = _timestamp(event.get("timestamp"))
        data = event.get("data")
        if timestamp is None or not start <= timestamp <= end or not isinstance(data, dict):
            ignored += 1
            continue

        zone_targets, transition = [], None
        if event_type == "DevicePropertyUpdatedEvent":
            device_id = _identifier(data.get("id"))
            property_name = data.get("property")
            if device_id in by_head:
                mapped_head_device_events += 1
                if isinstance(property_name, str):
                    mapped_head_property_counts[property_name] += 1
                    if property_name in TRACKED_DEVICE_PROPERTIES:
                        tracked_property_events[property_name] += 1
            else:
                device_id = None
            if device_id is None or property_name not in TRACKED_DEVICE_PROPERTIES:
                ignored += 1
                continue
            old_value, new_value = data.get("oldValue"), data.get("newValue")
            if not isinstance(old_value, bool) or not isinstance(new_value, bool):
                ignored += 1
                continue
            tracked_property_valid_transitions[property_name] += 1
            zone_targets = by_head[device_id]
            transition = {"kind": property_name, "head_id": device_id,
                          "old_value": old_value, "new_value": new_value}
        elif event_type == "ClimateZoneTemperatureChangedEvent":
            zone_id = _identifier(data.get("id") or data.get("zoneId"))
            if zone_id not in by_zone:
                ignored += 1
                continue
            old_value, new_value = _number(data.get("oldValue")), _number(data.get("newValue"))
            if old_value is None or new_value is None:
                ignored += 1
                continue
            zone_targets = [zone_id]
            transition = {"kind": "temperature", "old_value_c": old_value,
                          "new_value_c": new_value,
                          "unit": data.get("unit") if isinstance(data.get("unit"), str) else None}
        else:
            ignored += 1
            continue

        recognized += 1
        valid_timestamps.add(timestamp)
        event_id = event.get("id") if isinstance(event.get("id"), int) and not isinstance(event.get("id"), bool) else None
        for zone_id in zone_targets:
            zone_transitions[zone_id].append({
                "event_id": event_id, "timestamp": timestamp, "at": _iso(timestamp), **transition
            })

    output_zones, total_transitions, suspected_spikes = [], 0, 0
    for zone_id, zone in sorted(by_zone.items()):
        transitions = sorted(zone_transitions[zone_id],
                             key=lambda item: (item["timestamp"], item["event_id"] or 0))
        suspected_spikes += _temperature_spike_flags(transitions)
        total_transitions += len(transitions)
        output_zones.append({
            "id": zone_id, "name": zone.get("name"),
            "transition_count": len(transitions),
            "heat_required_transitions": sum(x["kind"] == "heatRequired" for x in transitions),
            "window_opened_transitions": sum(x["kind"] == "windowOpened" for x in transitions),
            "temperature_transitions": sum(x["kind"] == "temperature" for x in transitions),
            "suspected_temperature_spikes": sum(x.get("quality") == "suspected_transient_spike" for x in transitions),
            "transitions": transitions[:MAX_TRANSITIONS],
            "transitions_truncated": len(transitions) > MAX_TRANSITIONS,
        })

    heat_events = tracked_property_events["heatRequired"]
    heat_valid = tracked_property_valid_transitions["heatRequired"]
    if heat_valid:
        heat_status = "transitions_observed"
        heat_reason = "HC3 history contains boolean heatRequired transitions for mapped heads."
    elif heat_events:
        heat_status = "malformed_or_non_boolean_events"
        heat_reason = "heatRequired property events exist for mapped heads, but oldValue/newValue are not both booleans."
    else:
        heat_status = "no_events_observed"
        heat_reason = (
            "No heatRequired property-update event was found for mapped climate-zone heads in this window. "
            "Current snapshot state is reported separately; missing history events do not mean the property "
            "is unsupported or always false."
        )

    ordered_times = sorted(valid_timestamps)
    return {
        "schema": "thermoflow-fibaro-event-history/1",
        "source": "fibaro-hc3-event-history",
        "mode": "on_demand_read_only",
        "window": {"start": start, "end": end, "start_at": _iso(start),
                   "end_at": _iso(end), "duration_seconds": end - start},
        "counts": {"zones": len(output_zones), "events_received": len(events),
                   "events_recognized": recognized, "events_ignored": ignored,
                   "transitions": total_transitions},
        "event_audit": {
            "event_type_counts": dict(sorted(event_type_counts.items())),
            "mapped_head_device_property_events": mapped_head_device_events,
            "mapped_head_property_counts": dict(sorted(mapped_head_property_counts.items())),
            "tracked_property_events": {name: tracked_property_events[name] for name in sorted(TRACKED_DEVICE_PROPERTIES)},
            "tracked_property_valid_transitions": {name: tracked_property_valid_transitions[name] for name in sorted(TRACKED_DEVICE_PROPERTIES)},
            "heat_required_history": {
                "status": heat_status, "reason": heat_reason,
                "current_snapshot_state": _snapshot_heat_required(snapshot),
            },
        },
        "temperature_quality": {
            "status": "heuristic_flags_available",
            "suspected_transient_spikes": suspected_spikes,
            "rule": "Flag a jump >=5 C followed within 15 minutes by a return within 1 C of the prior value. Raw transitions are retained and marked.",
        },
        "coverage": {
            "status": "event_transitions_only" if recognized else "no_recognized_events",
            "first_event_at": _iso(ordered_times[0]) if ordered_times else None,
            "last_event_at": _iso(ordered_times[-1]) if ordered_times else None,
            "distinct_event_timestamps": len(ordered_times),
            "state_durations_available": False, "gaps_in_state": "not_inferred",
            "reason": "HC3 event history does not guarantee an initial state for the requested window; intervals and durations are therefore not inferred.",
        },
        "zones": output_zones,
        "safety": {"read_only": True, "raw_history_persisted": False,
                   "background_polling": False, "device_commands_sent": False},
    }
