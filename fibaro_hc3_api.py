"""Read-only HC3 snapshot shaping for the ThermoFlow API."""

from __future__ import annotations

from datetime import datetime, timezone


ZONE_PROPERTY_FIELDS = (
    "mode",
    "handSetPointHeating",
    "handSetPointCooling",
    "handTimestamp",
    "handMode",
    "vacationTemperature",
    "vacationMode",
    "currentTemperature",
    "currentTemperatureHeating",
    "currentTemperatureCooling",
    "currentSetpointHeating",
    "currentSetpointCooling",
    "calculationStrategy",
    "autoSetPointHysteresis",
    "devices",
    "temperatureSensors",
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
        heads.append({
            "id": device_id,
            "name": device.get("name"),
            "type": device.get("type"),
            "baseType": device.get("baseType"),
            "roomID": device.get("roomID"),
            "enabled": device.get("enabled"),
            "visible": device.get("visible"),
            "properties": _selected(device.get("properties"), HEAD_PROPERTY_FIELDS),
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
