"""Read-only Fibaro zone demand features for ThermoFlow.

The module interprets one HC3 snapshot only. It does not infer demand from
temperature error, does not create a time series, and never controls devices.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _boolean(value):
    """Normalize explicit HC3 booleans; missing or malformed values stay unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _tri_state_any(values, *, complete=True):
    """Any ON is positive evidence; OFF requires complete, all-OFF coverage."""
    normalized = [_boolean(value) for value in values]
    if any(value is True for value in normalized):
        return True
    if complete and normalized and all(value is False for value in normalized):
        return False
    return None


def _zone_temperature(properties):
    """Use the zone's current temperature; heating reference is separate."""
    return _number(properties.get("currentTemperature"))


def _head_consistency(properties):
    heat_required = _boolean(properties.get("heatRequired"))
    parameter3_heat_required = _boolean(properties.get("parameter3_heat_required"))
    if heat_required is None or parameter3_heat_required is None:
        return "unknown"
    return "consistent" if heat_required == parameter3_heat_required else "mismatch"


def build_fibaro_zone_diagnostics(snapshot: dict) -> dict:
    """Build conservative per-zone demand diagnostics from one HC3 snapshot.

    Prefer currentSetpointHeating when HC3 provides it. On HC3 climate zones
    where that field is absent, currentTemperatureHeating is the active
    scheduled heating setpoint: the supplied audit matched it to the active
    schedule slot in all ten zones. handSetPointHeating remains excluded
    because it is a manual override value, not the active scheduled target.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Nieprawidłowy snapshot Fibaro HC3")
    zones = snapshot.get("zones")
    heads = snapshot.get("heads")
    if not isinstance(zones, list) or not isinstance(heads, list):
        raise ValueError("Snapshot Fibaro HC3 nie zawiera list stref i głowic")

    heads_by_id = {}
    for head in heads:
        if not isinstance(head, dict):
            continue
        identifier = head.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            continue
        heads_by_id[identifier] = head

    head_results = []
    for identifier, head in sorted(heads_by_id.items()):
        properties = head.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        heat_required = _boolean(properties.get("heatRequired"))
        parameter2 = properties.get("parameter2")
        if isinstance(parameter2, bool):
            parameter2 = None
        try:
            parameter2 = int(parameter2) if parameter2 is not None else None
        except (TypeError, ValueError, OverflowError):
            parameter2 = None
        moderate_regulator = _boolean(properties.get("parameter2_moderate_regulator"))
        parameter3 = properties.get("parameter3")
        if isinstance(parameter3, bool):
            parameter3 = None
        try:
            parameter3 = int(parameter3) if parameter3 is not None else None
        except (TypeError, ValueError, OverflowError):
            parameter3 = None
        parameter3_heat_required = _boolean(
            properties.get("parameter3_heat_required")
        )
        head_results.append({
            "id": identifier,
            "name": head.get("name"),
            "heat_required": heat_required,
            "parameter2": parameter2,
            "moderate_regulator": moderate_regulator,
            "parameter3": parameter3,
            "parameter3_heat_required": parameter3_heat_required,
            "demand_consistency": _head_consistency(properties),
        })

    head_results_by_id = {item["id"]: item for item in head_results}
    zone_results = []
    features = []
    demand_count = 0
    no_demand_count = 0
    unknown_demand_count = 0
    window_open_count = 0
    temperature_pairs = 0

    for zone in zones:
        if not isinstance(zone, dict):
            continue
        properties = zone.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        raw_ids = properties.get("devices")
        head_ids = sorted({
            value for value in raw_ids
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }) if isinstance(raw_ids, list) else []
        zone_heads = [heads_by_id[item] for item in head_ids if item in heads_by_id]
        head_properties = [
            head.get("properties") if isinstance(head.get("properties"), dict) else {}
            for head in zone_heads
        ]

        head_coverage_complete = bool(head_ids) and len(zone_heads) == len(head_ids)
        heat_required = _tri_state_any(
            (_boolean(item.get("heatRequired")) for item in head_properties),
            complete=head_coverage_complete,
        )
        window_open = _tri_state_any(
            (_boolean(item.get("windowOpened")) for item in head_properties),
            complete=head_coverage_complete,
        )
        zone_head_results = [
            head_results_by_id[item] for item in head_ids
            if item in head_results_by_id
        ]
        head_consistency_values = [
            item["demand_consistency"] for item in zone_head_results
        ]
        if "mismatch" in head_consistency_values:
            demand_consistency = "mismatch"
        elif (head_coverage_complete and head_consistency_values
              and all(value == "consistent" for value in head_consistency_values)):
            demand_consistency = "consistent"
        else:
            demand_consistency = "unknown"
        if heat_required is True:
            demand_status = "required"
            demand_count += 1
        elif heat_required is False:
            demand_status = "not_required"
            no_demand_count += 1
        else:
            demand_status = "unknown"
            unknown_demand_count += 1

        if window_open is True:
            window_open_count += 1

        current_temperature = _zone_temperature(properties)
        current_temperature_heating = _number(
            properties.get("currentTemperatureHeating")
        )
        active_setpoint = _number(properties.get("currentSetpointHeating"))
        active_setpoint_source = "currentSetpointHeating"
        if active_setpoint is None:
            active_setpoint = current_temperature_heating
            active_setpoint_source = (
                "currentTemperatureHeating" if active_setpoint is not None else None
            )
        hysteresis = _number(properties.get("autoSetPointHysteresis"))
        temperature_gap = None
        temperature_relation = "unknown"
        gap_vs_hysteresis = "unknown"
        if current_temperature is not None and active_setpoint is not None:
            temperature_gap = round(active_setpoint - current_temperature, 2)
            temperature_relation = (
                "below_active_setpoint" if temperature_gap > 0.3
                else "at_or_above_active_setpoint"
            )
            if hysteresis is not None and hysteresis >= 0:
                gap_vs_hysteresis = (
                    "gap_exceeds_hysteresis"
                    if temperature_gap > hysteresis
                    else "within_or_below_hysteresis"
                )
            temperature_pairs += 1

        zone_id = zone.get("id")
        zone_results.append({
            "id": zone_id,
            "name": zone.get("name"),
            "active": zone.get("active"),
            "mode": zone.get("mode") or properties.get("mode"),
            "current_temperature_c": current_temperature,
            "current_temperature_heating_c": current_temperature_heating,
            "active_setpoint_c": active_setpoint,
            "active_setpoint_source": active_setpoint_source,
            "hysteresis_c": hysteresis,
            "temperature_gap_c": temperature_gap,
            "temperature_relation": temperature_relation,
            "gap_vs_hysteresis": gap_vs_hysteresis,
            "heat_demand": demand_status,
            "heat_required": heat_required,
            "window_open": window_open,
            "head_count": len(zone_heads),
            "head_count_reported": len(head_ids),
            "head_count_with_demand_state": sum(
                _boolean(item.get("heatRequired")) is not None for item in head_properties
            ),
            "head_count_with_parameter3": sum(
                item.get("parameter3") is not None for item in zone_head_results
            ),
            "heat_demand_consistency": demand_consistency,
            "head_ids": head_ids,
        })
        features.append({
            "zone_id": zone_id,
            "current_temperature_c": current_temperature,
            "current_temperature_heating_c": current_temperature_heating,
            "active_setpoint_c": active_setpoint,
            "active_setpoint_source": active_setpoint_source,
            "hysteresis_c": hysteresis,
            "temperature_gap_c": temperature_gap,
            "gap_vs_hysteresis": gap_vs_hysteresis,
            "heat_required": heat_required,
            "heat_demand_consistency": demand_consistency,
            "window_open": window_open,
            "head_count": len(zone_heads),
        })

    return {
        "schema": "thermoflow-fibaro-zone-diagnostics/1",
        "source": "fibaro-hc3",
        "mode": "read_only_snapshot",
        "fetched_at": snapshot.get("fetched_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "zones": len(zone_results),
            "heads": len(heads_by_id),
            "zones_with_heat_demand": demand_count,
            "zones_without_heat_demand": no_demand_count,
            "zones_with_unknown_heat_demand": unknown_demand_count,
            "zones_with_open_window": window_open_count,
            "zones_with_temperature_pair": temperature_pairs,
            "heads_with_heat_demand": sum(item["heat_required"] is True for item in head_results),
            "heads_without_heat_demand": sum(item["heat_required"] is False for item in head_results),
            "heads_with_unknown_heat_demand": sum(item["heat_required"] is None for item in head_results),
            "heads_with_demand_source_mismatch": sum(item["demand_consistency"] == "mismatch" for item in head_results),
            "heads_with_consistent_demand_sources": sum(item["demand_consistency"] == "consistent" for item in head_results),
            "heads_with_unknown_demand_consistency": sum(item["demand_consistency"] == "unknown" for item in head_results),
            "heads_with_moderate_regulator_enabled": sum(item["moderate_regulator"] is True for item in head_results),
            "heads_with_moderate_regulator_disabled": sum(item["moderate_regulator"] is False for item in head_results),
            "heads_with_unknown_moderate_regulator": sum(item["moderate_regulator"] is None for item in head_results),
        },
        "heads": head_results,
        "zones": zone_results,
        "features": features,
        "feature_readiness": {
            "status": "snapshot_only",
            "training_ready": False,
            "reason": (
                "Pojedynczy snapshot nie zawiera historii czasowej; "
                "nie należy używać go samodzielnie do trenowania ML."
            ),
        },
        "interpretation": {
            "heat_demand_source": "FGT001 heatRequired boolean state (primary)",
            "parameter3_cross_check": (
                "Z-Wave parameter 3 bit 4 is reported as a consistency check; "
                "it is not treated as an independent demand source."
            ),
            "temperature_source": "climate zone currentTemperature only",
            "current_temperature_heating": (
                "HC3 currentTemperatureHeating matched the active schedule "
                "temperatureHeating in all ten zones in the supplied audit; "
                "used as a fallback active heating setpoint only when "
                "currentSetpointHeating is absent."
            ),
            "setpoint_source": (
                "currentSetpointHeating, falling back to currentTemperatureHeating"
            ),
            "hysteresis": (
                "autoSetPointHysteresis is reported as context only; the "
                "temperature gap is not used to infer head demand."
            ),
            "manual_setpoint_excluded": "handSetPointHeating",
            "temperature_gap_is_not_heat_demand": True,
            "causality": "not_established",
        },
        "safety": (
            "read_only; no HC3 commands; no boiler, pump, valve or setpoint changes; "
            "return protection remains 55°C ±1°C"
        ),
    }
