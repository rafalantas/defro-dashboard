"""Canonical ThingSpeak compact-channel contract for ThermoFlow.

Field numbers are 1-based and are the single source of truth for API writes,
history reads, fuel counter extraction and telemetry normalization.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/Warsaw"
VALVE_OPENING_SERIES_ID = "thermoflow-valve4d-opening"
VALVE_OPENING_SERIES_LABEL = "Otwarcie zaworu 4D"
VALVE_SETPOINT_SERIES_ID = "thermoflow-valve4d-setpoint"
VALVE_SETPOINT_SERIES_LABEL = "Temperatura zadana zaworu 4D"

# (key, label, stable_series_id, value_kind, snapshot_path)
CHANNEL_FIELDS = (
    {
        "name": "temperatures_power",
        "fields": (
            ("co_c", "Temperatura CO", "thermoflow-co", "temperature", "temperatures.co"),
            ("dhw_c", "Temperatura CWU", "thermoflow-dhw", "temperature", "temperatures.dhw"),
            ("flue_c", "Temperatura spalin", "thermoflow-flue", "temperature", "temperatures.flue"),
            ("outdoor_c", "Temperatura zewnętrzna", "thermoflow-outdoor", "temperature", "temperatures.outdoor"),
            ("room_c", "Temperatura pokoju", "thermoflow-room", "temperature", "temperatures.room"),
            ("floor_c", "Temperatura podłogówki (C1)", "thermoflow-floor", "temperature", "temperatures.buffer_upper"),
            ("valve_current_c", "Temperatura zaworu 4D", "thermoflow-valve4d-current", "temperature", "valve.current"),
            ("boiler_power_kw", "Moc chwilowa pieca", "thermoflow-boiler-power", "power", "boiler_power_kw"),
        ),
    },
    {
        "name": "valve_pumps_fuel",
        "fields": (
            ("valve_setpoint_c", "Temperatura zadana zaworu 4D po korekcie", "thermoflow-valve4d-effective-setpoint", "temperature", "valve.set_after_correction"),
            ("valve_opening_pct", VALVE_OPENING_SERIES_LABEL, VALVE_OPENING_SERIES_ID, "percent", "valve.opening"),
            ("return_c", "Temperatura powrotu", "thermoflow-valve4d-return", "temperature", "valve.return"),
            ("pump_valve4d", "Pompa zaworu 4D", "thermoflow-pump-valve4d", "state", "pumps.valve4d"),
            ("pump_floor", "Pompa podłogowa", "thermoflow-pump-floor", "state", "pumps.floor"),
            ("pump_dhw", "Pompa CWU", "thermoflow-pump-dhw", "state", "pumps.dhw"),
            ("pump_circulation", "Pompa cyrkulacyjna", "thermoflow-pump-circulation", "state", "pumps.circulation"),
            ("fuel_percent", "Zapas paliwa", "thermoflow-fuel-percent", "percent", "fuel.percent"),
        ),
    },
)


def api_channel_fields() -> tuple:
    """Return the legacy api.py tuple shape without duplicating field metadata."""
    return tuple(
        tuple((series_id, label, path) for _key, label, series_id, _kind, path in channel["fields"])
        for channel in CHANNEL_FIELDS
    )


def telemetry_channels() -> tuple:
    """Return the legacy fuel_telemetry.CHANNELS shape from this contract."""
    return tuple({
        "name": channel["name"],
        "series": tuple((key, label, series_id, kind)
                        for key, label, series_id, kind, _path in channel["fields"]),
    } for channel in CHANNEL_FIELDS)


def field_number(key: str, channel_name: str) -> int:
    """Return a 1-based ThingSpeak field number for a semantic key and channel."""
    for channel in CHANNEL_FIELDS:
        if channel["name"] != channel_name:
            continue
        for index, field in enumerate(channel["fields"], start=1):
            if field[0] == key:
                return index
    raise KeyError(f"Unknown ThingSpeak field: {channel_name}.{key}")


def timezone():
    """Shared local timezone; defaults to the existing Europe/Warsaw behavior."""
    return ZoneInfo(os.getenv("FUEL_TZ", DEFAULT_TIMEZONE))
