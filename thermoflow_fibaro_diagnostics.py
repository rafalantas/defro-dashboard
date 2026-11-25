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


def _tri_state_any(values):
    """Return True/False only when boolean evidence is present; else unknown."""
    known = [value for value in values if isinstance(value, bool)]
    if any(known):
        return True
    if known:
        return False
    return None


def _zone_temperature(properties):
    return _number(
        properties.get("currentTemperatureHeating")
        if properties.get("currentTemperatureHeating") is not None
        else properties.get("currentTemperature")
    )


def build_fibaro_zone_diagnostics(snapshot: dict) -> dict:
    """Build conservative per-zone demand diagnostics from one HC3 snapshot.

    Only the active zone setpoint (currentSetpointHeating) is used. The
    handSetPointHeating property is intentionally excluded because it is a
    manual setpoint, not necessarily the active scheduled setpoint.
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

        heat_required = _tri_state_any(
            item.get("heatRequired") for item in head_properties
        )
        window_open = _tri_state_any(
            item.get("windowOpened") for item in head_properties
        )
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
        active_setpoint = _number(properties.get("currentSetpointHeating"))
        temperature_gap = None
        temperature_relation = "unknown"
        if current_temperature is not None and active_setpoint is not None:
            temperature_gap = round(active_setpoint - current_temperature, 2)
            temperature_relation = (
                "below_active_setpoint" if temperature_gap > 0.3
                else "at_or_above_active_setpoint"
            )
            temperature_pairs += 1

        zone_id = zone.get("id")
        zone_results.append({
            "id": zone_id,
            "name": zone.get("name"),
            "active": zone.get("active"),
            "mode": zone.get("mode") or properties.get("mode"),
            "current_temperature_c": current_temperature,
            "active_setpoint_c": active_setpoint,
            "temperature_gap_c": temperature_gap,
            "temperature_relation": temperature_relation,
            "heat_demand": demand_status,
            "heat_required": heat_required,
            "window_open": window_open,
            "head_count": len(zone_heads),
            "head_count_reported": len(head_ids),
            "head_count_with_demand_state": sum(
                isinstance(item.get("heatRequired"), bool) for item in head_properties
            ),
            "head_ids": head_ids,
        })
        features.append({
            "zone_id": zone_id,
            "current_temperature_c": current_temperature,
            "active_setpoint_c": active_setpoint,
            "temperature_gap_c": temperature_gap,
            "heat_required": heat_required,
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
        },
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
            "heat_demand_source": "FGT001 heatRequired boolean state",
            "temperature_source": "climate zone currentTemperatureHeating/currentTemperature",
            "setpoint_source": "climate zone currentSetpointHeating only",
            "manual_setpoint_excluded": "handSetPointHeating",
            "temperature_gap_is_not_heat_demand": True,
            "causality": "not_established",
        },
        "safety": (
            "read_only; no HC3 commands; no boiler, pump, valve or setpoint changes; "
            "return protection remains 55°C ±1°C"
        ),
    }
