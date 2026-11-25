"""On-demand, read-only analysis of Fibaro HC3 event history.

Only verified HC3 event shapes are interpreted. This module reports observed
transitions; it does not invent state between events or infer heat demand from
temperature differences.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite


MAX_HISTORY_SECONDS = 7 * 24 * 60 * 60
MAX_TRANSITIONS = 1000
TRACKED_DEVICE_PROPERTIES = {"heatRequired", "windowOpened"}


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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not isfinite(value):
        return None
    return int(value)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _zone_index(snapshot):
    zones = snapshot.get("zones") if isinstance(snapshot, dict) else None
    if not isinstance(zones, list):
        raise ValueError("Snapshot Fibaro HC3 nie zawiera listy stref")
    by_head = {}
    by_zone = {}
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


def analyze_fibaro_event_history(snapshot: dict, events: list, start: int, end: int) -> dict:
    """Summarize verified device-property and climate-zone temperature events."""
    if (isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int)
            or start >= end or end - start > MAX_HISTORY_SECONDS):
        raise ValueError("Zakres historii Fibaro HC3 musi wynosić od 1 sekundy do 7 dni")
    if not isinstance(events, list):
        raise ValueError("Historia zdarzeń Fibaro HC3 musi być listą")

    by_head, by_zone = _zone_index(snapshot)
    zone_transitions = {zone_id: [] for zone_id in by_zone}
    recognized = 0
    ignored = 0
    valid_timestamps = set()

    for event in events:
        if not isinstance(event, dict):
            ignored += 1
            continue
        timestamp = _timestamp(event.get("timestamp"))
        data = event.get("data")
        if timestamp is None or not start <= timestamp <= end or not isinstance(data, dict):
            ignored += 1
            continue
        event_type = event.get("type")
        zone_targets = []
        transition = None

        if event_type == "DevicePropertyUpdatedEvent":
            device_id = _identifier(data.get("id"))
            property_name = data.get("property")
            if device_id is None or property_name not in TRACKED_DEVICE_PROPERTIES:
                ignored += 1
                continue
            zone_targets = by_head.get(device_id, [])
            if not zone_targets:
                ignored += 1
                continue
            old_value, new_value = data.get("oldValue"), data.get("newValue")
            if not isinstance(old_value, bool) or not isinstance(new_value, bool):
                ignored += 1
                continue
            transition = {
                "kind": property_name,
                "head_id": device_id,
                "old_value": old_value,
                "new_value": new_value,
            }
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
            transition = {
                "kind": "temperature",
                "old_value_c": old_value,
                "new_value_c": new_value,
                "unit": data.get("unit") if isinstance(data.get("unit"), str) else None,
            }
        else:
            ignored += 1
            continue

        recognized += 1
        valid_timestamps.add(timestamp)
        for zone_id in zone_targets:
            zone_transitions[zone_id].append({
                "event_id": event.get("id") if isinstance(event.get("id"), int) else None,
                "timestamp": timestamp,
                "at": _iso(timestamp),
                **transition,
            })

    output_zones = []
    total_transitions = 0
    for zone_id, zone in sorted(by_zone.items()):
        transitions = sorted(
            zone_transitions[zone_id],
            key=lambda item: (item["timestamp"], item["event_id"] or 0),
        )
        total_transitions += len(transitions)
        output_zones.append({
            "id": zone_id,
            "name": zone.get("name"),
            "transition_count": len(transitions),
            "heat_required_transitions": sum(item["kind"] == "heatRequired" for item in transitions),
            "window_opened_transitions": sum(item["kind"] == "windowOpened" for item in transitions),
            "temperature_transitions": sum(item["kind"] == "temperature" for item in transitions),
            "transitions": transitions[:MAX_TRANSITIONS],
            "transitions_truncated": len(transitions) > MAX_TRANSITIONS,
        })

    ordered_times = sorted(valid_timestamps)
    return {
        "schema": "thermoflow-fibaro-event-history/1",
        "source": "fibaro-hc3-event-history",
        "mode": "on_demand_read_only",
        "window": {"start": start, "end": end, "start_at": _iso(start), "end_at": _iso(end),
                   "duration_seconds": end - start},
        "counts": {
            "zones": len(output_zones),
            "events_received": len(events),
            "events_recognized": recognized,
            "events_ignored": ignored,
            "transitions": total_transitions,
        },
        "coverage": {
            "status": "event_transitions_only" if recognized else "no_recognized_events",
            "first_event_at": _iso(ordered_times[0]) if ordered_times else None,
            "last_event_at": _iso(ordered_times[-1]) if ordered_times else None,
            "distinct_event_timestamps": len(ordered_times),
            "state_durations_available": False,
            "gaps_in_state": "not_inferred",
            "reason": "HC3 event history does not guarantee an initial state for the requested window; intervals and durations are therefore not inferred.",
        },
        "zones": output_zones,
        "safety": {
            "read_only": True,
            "raw_history_persisted": False,
            "background_polling": False,
            "device_commands_sent": False,
        },
    }
