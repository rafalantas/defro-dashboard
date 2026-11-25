"""Snapshot-based Fibaro telemetry contract for ThermoFlow AI (shadow only).

This module validates one freshly fetched HC3 snapshot and projects bounded,
explicitly tri-state features. It does not persist data, poll HC3, infer demand
from temperature error, or affect the production fuel forecast.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import isfinite


SCHEMA = "thermoflow-fibaro-telemetry/1"
DEFAULT_EXPECTED_HEAD_COUNT = 15
DEFAULT_EXPECTED_PARAMETER2 = 322
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 180
LOW_BATTERY_THRESHOLD_PERCENT = 20


def _identifier(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result <= 0 or str(result) != str(value).strip():
        return None
    return result


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, str) and str(result) != value.strip():
        return None
    return result


def _parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _snapshot_freshness(snapshot, now, max_age_seconds):
    fetched = _parse_timestamp(snapshot.get("fetched_at"))
    if fetched is None:
        return {"status": "unknown", "age_seconds": None, "max_age_seconds": max_age_seconds}
    age = (now - fetched).total_seconds()
    if age < -30:
        status = "invalid_future_timestamp"
    elif age > max_age_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "age_seconds": round(max(0.0, age), 1),
        "max_age_seconds": max_age_seconds,
        "fetched_at": fetched.isoformat(),
    }


def _valid_expected(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Nieprawidłowa konfiguracja {name}")
    return value


def build_fibaro_telemetry(
    snapshot: dict,
    diagnostics: dict,
    *,
    now: datetime | None = None,
    expected_head_count: int = DEFAULT_EXPECTED_HEAD_COUNT,
    expected_parameter2: int = DEFAULT_EXPECTED_PARAMETER2,
    max_snapshot_age_seconds: int = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> dict:
    """Build the stable, read-only Fibaro feature contract for downstream AI."""
    if not isinstance(snapshot, dict):
        raise ValueError("Nieprawidłowy snapshot Fibaro HC3")
    zones = snapshot.get("zones")
    heads = snapshot.get("heads")
    if not isinstance(zones, list) or not isinstance(heads, list):
        raise ValueError("Snapshot Fibaro HC3 nie zawiera list stref i głowic")
    if not isinstance(diagnostics, dict) or not isinstance(diagnostics.get("zones"), list):
        raise ValueError("Brak diagnostyki stref Fibaro HC3")

    expected_head_count = _valid_expected(
        expected_head_count, "oczekiwanej liczby głowic", 1
    )
    expected_parameter2 = _valid_expected(
        expected_parameter2, "oczekiwanej wartości parametru P2", 0
    )
    max_snapshot_age_seconds = _valid_expected(
        max_snapshot_age_seconds, "maksymalnego wieku snapshotu", 1
    )
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Czas oceny Fibaro musi zawierać strefę czasową")
    now = now.astimezone(timezone.utc)

    zone_by_id = {}
    device_zones = {}
    invalid_mapping_entries = 0
    for zone in zones:
        if not isinstance(zone, dict):
            invalid_mapping_entries += 1
            continue
        zone_id = _identifier(zone.get("id"))
        if zone_id is None:
            invalid_mapping_entries += 1
            continue
        zone_by_id[zone_id] = zone
        properties = zone.get("properties")
        device_ids = properties.get("devices") if isinstance(properties, dict) else None
        if not isinstance(device_ids, list):
            continue
        for raw_id in device_ids:
            device_id = _identifier(raw_id)
            if device_id is None:
                invalid_mapping_entries += 1
                continue
            device_zones.setdefault(device_id, []).append(zone_id)

    heads_by_id = {}
    duplicate_head_ids = []
    for head in heads:
        if not isinstance(head, dict):
            continue
        device_id = _identifier(head.get("id"))
        if device_id is None:
            continue
        if device_id in heads_by_id:
            duplicate_head_ids.append(device_id)
            continue
        heads_by_id[device_id] = head

    mapped_ids = set(device_zones)
    observed_ids = set(heads_by_id)
    duplicate_zone_mappings = {
        str(device_id): sorted(set(zone_ids))
        for device_id, zone_ids in device_zones.items()
        if len(set(zone_ids)) > 1
    }
    unresolved_mapped_ids = sorted(mapped_ids - observed_ids)
    unmapped_head_ids = sorted(observed_ids - mapped_ids)
    mapped_observed_ids = mapped_ids & observed_ids
    expected_count_matches = len(observed_ids) == expected_head_count
    mapping_complete = (
        expected_count_matches
        and not duplicate_head_ids
        and not duplicate_zone_mappings
        and not unresolved_mapped_ids
        and not unmapped_head_ids
        and len(mapped_observed_ids) == expected_head_count
    )
    mapping = {
        "status": "complete" if mapping_complete else "incomplete",
        "expected_fgt001_heads": expected_head_count,
        "observed_fgt001_heads": len(observed_ids),
        "mapped_fgt001_heads": len(mapped_observed_ids),
        "mapped_device_ids": sorted(mapped_ids),
        "unresolved_mapped_device_ids": unresolved_mapped_ids,
        "unmapped_fgt001_head_ids": unmapped_head_ids,
        "duplicate_head_ids": sorted(set(duplicate_head_ids)),
        "heads_mapped_to_multiple_zones": duplicate_zone_mappings,
        "invalid_mapping_entries": invalid_mapping_entries,
        "coverage_percent": round(
            100.0 * len(mapped_observed_ids) / expected_head_count, 1
        ),
    }

    diagnostic_zones = {}
    for item in diagnostics["zones"]:
        if not isinstance(item, dict):
            continue
        zone_id = _identifier(item.get("id"))
        if zone_id is not None:
            diagnostic_zones[zone_id] = item
    head_results = []
    for device_id, head in sorted(heads_by_id.items()):
        properties = head.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        battery = _number(properties.get("batteryLevel"))
        if battery is not None and not 0 <= battery <= 100:
            battery = None
        parameter2 = _integer(properties.get("parameter2"))
        heat_required = _boolean(properties.get("heatRequired"))
        window_open = _boolean(properties.get("windowOpened"))
        dead = _boolean(properties.get("dead"))
        if dead is True:
            communication = "offline"
        elif dead is False:
            communication = "online"
        else:
            communication = "unknown"
        if battery is None:
            battery_status = "unknown"
        elif battery <= LOW_BATTERY_THRESHOLD_PERCENT:
            battery_status = "low"
        else:
            battery_status = "normal"

        zone_ids = sorted(set(device_zones.get(device_id, [])))
        zone_names = [
            zone_by_id[zone_id].get("name")
            for zone_id in zone_ids
            if zone_id in zone_by_id and isinstance(zone_by_id[zone_id].get("name"), str)
        ]
        head_results.append({
            "id": device_id,
            "name": head.get("name"),
            "zone_ids": zone_ids,
            "zone_names": zone_names,
            "heat_required": heat_required,
            "window_open": window_open,
            "battery_percent": battery,
            "battery_status": battery_status,
            "communication": communication,
            "parameter2": parameter2,
            "parameter2_expected": expected_parameter2,
            "parameter2_matches_expected": (
                parameter2 == expected_parameter2 if parameter2 is not None else None
            ),
            "moderate_regulator": (
                bool(parameter2 & 64) if parameter2 is not None else None
            ),
            "demand_consistency": (
                properties.get("heat_demand_consistency")
                if properties.get("heat_demand_consistency") in (
                    "consistent", "mismatch", "unknown"
                ) else "unknown"
            ),
        })

    def count_state(key, value):
        return sum(item[key] is value for item in head_results)

    zone_results = []
    for zone_id, item in sorted(diagnostic_zones.items()):
        zone_results.append({
            "id": zone_id,
            "name": item.get("name"),
            "current_temperature_c": item.get("current_temperature_c"),
            "active_setpoint_c": item.get("active_setpoint_c"),
            "active_setpoint_source": item.get("active_setpoint_source"),
            "temperature_gap_c": item.get("temperature_gap_c"),
            "temperature_relation": item.get("temperature_relation", "unknown"),
            "heat_demand": item.get("heat_demand", "unknown"),
            "heat_required": item.get("heat_required"),
            "window_open": item.get("window_open"),
            "head_count": item.get("head_count", 0),
            "head_count_reported": item.get("head_count_reported", 0),
        })

    heat_demand_count = Counter(item["heat_demand"] for item in zone_results)
    temperature_gaps = [
        value for value in (_number(item.get("temperature_gap_c")) for item in zone_results)
        if value is not None
    ]
    below_setpoint = sum(
        item.get("temperature_relation") == "below_active_setpoint"
        for item in zone_results
    )
    at_or_above_setpoint = sum(
        item.get("temperature_relation") == "at_or_above_active_setpoint"
        for item in zone_results
    )

    counts = {
        "zones": len(zone_results),
        "heads": len(head_results),
        "heads_with_heat_demand": count_state("heat_required", True),
        "heads_without_heat_demand": count_state("heat_required", False),
        "heads_with_unknown_heat_demand": count_state("heat_required", None),
        "heads_with_open_window": count_state("window_open", True),
        "heads_with_closed_window": count_state("window_open", False),
        "heads_with_unknown_window_state": count_state("window_open", None),
        "heads_online": sum(item["communication"] == "online" for item in head_results),
        "heads_offline": sum(item["communication"] == "offline" for item in head_results),
        "heads_with_unknown_communication": sum(item["communication"] == "unknown" for item in head_results),
        "heads_with_battery_data": sum(item["battery_percent"] is not None for item in head_results),
        "heads_with_unknown_battery": sum(item["battery_percent"] is None for item in head_results),
        "heads_with_low_battery": sum(item["battery_status"] == "low" for item in head_results),
        "heads_with_expected_parameter2": sum(item["parameter2_matches_expected"] is True for item in head_results),
        "heads_with_mismatched_parameter2": sum(item["parameter2_matches_expected"] is False for item in head_results),
        "heads_with_unknown_parameter2": sum(item["parameter2_matches_expected"] is None for item in head_results),
        "zones_with_heat_demand": heat_demand_count["required"],
        "zones_without_heat_demand": heat_demand_count["not_required"],
        "zones_with_unknown_heat_demand": heat_demand_count["unknown"],
        "zones_below_active_setpoint": below_setpoint,
        "zones_at_or_above_active_setpoint": at_or_above_setpoint,
    }

    freshness = _snapshot_freshness(snapshot, now, max_snapshot_age_seconds)
    if freshness["status"] == "stale":
        status = "stale"
    elif freshness["status"] != "fresh":
        status = "unknown"
    elif mapping["status"] != "complete" or counts["heads_offline"] or counts["heads_with_mismatched_parameter2"]:
        status = "degraded"
    elif (counts["heads_with_unknown_heat_demand"]
          or counts["heads_with_unknown_communication"]
          or counts["heads_with_unknown_parameter2"]):
        status = "partial"
    else:
        status = "ready"

    feature_values = {
        "zones_with_heat_demand": counts["zones_with_heat_demand"],
        "zones_without_heat_demand": counts["zones_without_heat_demand"],
        "zones_with_unknown_heat_demand": counts["zones_with_unknown_heat_demand"],
        "heads_with_heat_demand": counts["heads_with_heat_demand"],
        "heads_without_heat_demand": counts["heads_without_heat_demand"],
        "heads_with_unknown_heat_demand": counts["heads_with_unknown_heat_demand"],
        "heads_with_open_window": counts["heads_with_open_window"],
        "heads_offline": counts["heads_offline"],
        "heads_with_low_battery": counts["heads_with_low_battery"],
        "zones_below_active_setpoint": below_setpoint,
        "zones_at_or_above_active_setpoint": at_or_above_setpoint,
        "mean_temperature_gap_c": (
            round(sum(temperature_gaps) / len(temperature_gaps), 2)
            if temperature_gaps else None
        ),
    }

    return {
        "schema": SCHEMA,
        "source": "fibaro-hc3",
        "mode": "read_only_snapshot",
        "status": status,
        "fetched_at": snapshot.get("fetched_at"),
        "generated_at": now.isoformat(),
        "freshness": freshness,
        "mapping": mapping,
        "counts": counts,
        "configuration": {
            "expected_parameter2": expected_parameter2,
            "parameter2_semantics": {
                "moderate_regulator_mask": 64,
                "moderate_regulator_enabled": "P2 bit 6",
            },
            "low_battery_threshold_percent": LOW_BATTERY_THRESHOLD_PERCENT,
        },
        "heads": head_results,
        "zones": zone_results,
        "ai_shadow": {
            "schema": "thermoflow-fibaro-features/1",
            "status": "snapshot_only",
            "features": feature_values,
            "training_ready": False,
            "history_available": False,
            "reason": (
                "Pojedynczy snapshot nie tworzy szeregu czasowego; "
                "cechy są dostępne wyłącznie do diagnostyki i ewaluacji shadow."
            ),
        },
        "fuel_forecast": {
            "status": "unchanged",
            "applied": False,
            "reason": "Fibaro telemetry remains shadow-only and does not alter Fuel Forecast.",
        },
        "safety": {
            "read_only": True,
            "hc3_writes": False,
            "device_commands_sent": False,
            "boiler_pump_valve_or_setpoint_changes": False,
            "return_protection": "55°C ±1°C",
            "background_polling": False,
            "raw_history_persisted": False,
        },
    }
