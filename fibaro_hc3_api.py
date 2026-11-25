"""Read-only HC3 snapshot shaping for the ThermoFlow API."""

from __future__ import annotations

import json
from datetime import datetime, timezone


ZONE_PROPERTY_FIELDS = (
    "mode",
    "handSetPointHeating",
    "handSetPointCooling",
    "handTimestamp",
    "handMode",
    "vacationTemperature",
    "vacationSetPointHeating",
    "vacationSetPointCooling",
    "vacationMode",
    "vacationStartTime",
    "vacationEndTime",
    "currentTemperature",
    "currentTemperatureHeating",
    "currentTemperatureCooling",
    "currentSetpointHeating",
    "currentSetpointCooling",
    "currentSetpointTimeStartHour",
    "currentSetpointTimeStartMinute",
    "currentSetpointTimeEndHour",
    "currentSetpointTimeEndMinute",
    "calculationStrategy",
    "autoSetPointHysteresis",
    "devices",
    "temperatureSensors",
    # HC3 exposes one schedule object per weekday; preserve it without rewriting it.
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

HEAD_PROPERTY_FIELDS = (
    "heatRequired",
    "heatingThermostatSetpoint",
    "thermostatMode",
    "windowOpened",
    "batteryLevel",
    "value",
    "temperature",
    "dead",
    "lastBreached",
)


def _positive_ids(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            identifier = int(value)
        except (TypeError, ValueError):
            continue
        if identifier > 0 and str(identifier) == str(value).strip():
            result.append(identifier)
    return sorted(set(result))


def _selected(source, fields):
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in fields if key in source}


def _boolean_value(value):
    """Normalize HC3 boolean properties without guessing missing values."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _parameter_value(properties, parameter_id):
    """Read one numeric Z-Wave parameter from HC3's list or JSON-string form."""
    if not isinstance(properties, dict):
        return None
    parameters = properties.get("parameters")
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except (TypeError, ValueError):
            return None
    if not isinstance(parameters, list):
        return None
    for item in parameters:
        if not isinstance(item, dict) or item.get("id") != parameter_id:
            continue
        value = item.get("value")
        if isinstance(value, bool) or value is None:
            return None
        try:
            numeric = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        return numeric
    return None


def build_hc3_snapshot(provider) -> dict:
    """Fetch one on-demand snapshot; never poll or persist HC3 data."""
    overview = provider.get_climate_zones()
    if not isinstance(overview, list):
        raise ValueError("Nieprawidłowa lista stref Fibaro HC3")

    zones = []
    head_ids = set()
    for item in overview:
        if not isinstance(item, dict):
            continue
        zone_id = item.get("id")
        if isinstance(zone_id, bool) or not isinstance(zone_id, int) or zone_id <= 0:
            continue
        detail = provider.get_climate_zone(zone_id)
        if not isinstance(detail, dict):
            raise ValueError(f"Nieprawidłowe dane strefy Fibaro HC3: {zone_id}")
        properties = detail.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        overview_properties = item.get("properties")
        if not isinstance(overview_properties, dict):
            overview_properties = {}

        devices = _positive_ids(properties.get("devices"))
        if not devices:
            devices = _positive_ids(overview_properties.get("devices"))
        sensors = _positive_ids(properties.get("temperatureSensors"))
        if not sensors:
            sensors = _positive_ids(overview_properties.get("temperatureSensors"))
        head_ids.update(devices)

        selected_properties = _selected(properties, ZONE_PROPERTY_FIELDS)
        if devices:
            selected_properties["devices"] = devices
        if sensors:
            selected_properties["temperatureSensors"] = sensors
        zones.append({
            "id": zone_id,
            "name": detail.get("name") or item.get("name"),
            "active": detail.get("active", item.get("active")),
            "mode": detail.get("mode") or item.get("mode"),
            "properties": selected_properties,
        })

    heads = []
    for device_id in sorted(head_ids):
        device = provider.get_device(device_id)
        if not isinstance(device, dict):
            raise ValueError(f"Nieprawidłowe dane głowicy Fibaro HC3: {device_id}")
        # Climate-zone devices can include non-head devices on some HC3 setups.
        # Only physical FGT001 radiator heads are part of this demand snapshot.
        if device.get("type") != "com.fibaro.FGT001":
            continue

        raw_properties = device.get("properties")
        selected_properties = _selected(raw_properties, HEAD_PROPERTY_FIELDS)
        parameter3 = _parameter_value(raw_properties, 3)
        parameter3_heat_required = (
            bool(parameter3 & 4) if parameter3 is not None else None
        )
        heat_required = _boolean_value(selected_properties.get("heatRequired"))

        selected_properties["parameter3"] = parameter3
        selected_properties["parameter3_heat_required"] = parameter3_heat_required
        if heat_required is None or parameter3_heat_required is None:
            selected_properties["heat_demand_consistency"] = "unknown"
        elif heat_required == parameter3_heat_required:
            selected_properties["heat_demand_consistency"] = "consistent"
        else:
            selected_properties["heat_demand_consistency"] = "mismatch"

        heads.append({
            "id": device_id,
            "name": device.get("name"),
            "type": device.get("type"),
            "baseType": device.get("baseType"),
            "roomID": device.get("roomID"),
            "enabled": device.get("enabled"),
            "visible": device.get("visible"),
            "properties": selected_properties,
        })

    return {
        "schema": "fibaro-hc3-snapshot/1",
        "source": "fibaro-hc3",
        "configured": True,
        "connected": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"zones": len(zones), "heads": len(heads)},
        "zones": sorted(zones, key=lambda zone: zone["id"]),
        "heads": heads,
    }
