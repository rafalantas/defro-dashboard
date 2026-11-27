"""Read-only Fibaro dashboard projection from the shared SQLite archive."""
from __future__ import annotations

import math
import time
from pathlib import Path

from thermoflow_fibaro_diagnostics import zone_reading
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

MAX_DAYS = 14
MAX_EVENTS = 100000


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and str(parsed) == str(value).strip() else None


def read_fibaro_dashboard(database_path=None, *, days=14, now=None):
    """Return bounded zone snapshot and event-native temperature history.

    This reads the already-collected SQLite archive only. It never contacts HC3,
    infers historical head-to-zone membership, writes data, or sends commands.
    """
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError("days must be between 1 and 14")
    stamp = int(time.time()) if now is None else now
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    start = max(1, stamp - days * 86400)
    path = Path(database_path or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")

    store = SQLiteStore(path)
    collector = FibaroHistoryCollector(store)
    snapshots = collector.read_snapshots(start=start, end=stamp, limit=10000)
    events = collector.read_events(start=start, end=stamp, limit=MAX_EVENTS)
    latest = max(snapshots, key=lambda item: (item["captured_at"], item["id"])) if snapshots else None
    snapshot = latest.get("payload") if latest else None
    if not isinstance(snapshot, dict):
        snapshot = {}

    heads = {
        _positive_id(head.get("id")): head
        for head in snapshot.get("heads", [])
        if isinstance(head, dict) and _positive_id(head.get("id")) is not None
    }
    zones = []
    for zone in snapshot.get("zones", []):
        if not isinstance(zone, dict):
            continue
        zone_id = _positive_id(zone.get("id"))
        if zone_id is None:
            continue
        props = zone.get("properties") if isinstance(zone.get("properties"), dict) else {}
        device_ids = sorted({
            device_id for raw_id in (props.get("devices") or [])
            if (device_id := _positive_id(raw_id)) is not None
        }) if isinstance(props.get("devices") or [], list) else []
        zone_heads = [heads[item] for item in device_ids if item in heads]
        head_summary = []
        for head in zone_heads:
            hp = head.get("properties") if isinstance(head.get("properties"), dict) else {}
            head_summary.append({
                "id": head.get("id"),
                "name": head.get("name"),
                "battery": _number(hp.get("batteryLevel")),
                "dead": hp.get("dead") if isinstance(hp.get("dead"), bool) else None,
                "heat_required": hp.get("heatRequired") if isinstance(hp.get("heatRequired"), bool) else None,
                "window_open": hp.get("windowOpened") if isinstance(hp.get("windowOpened"), bool) else None,
            })
        reading = zone_reading(props)   # shared rule: measured temperature vs active setpoint
        zones.append({
            "id": zone_id,
            "name": str(zone.get("name") or f"Strefa {zone_id}"),
            "active": zone.get("active") if isinstance(zone.get("active"), bool) else None,
            "mode": zone.get("mode"),
            "temperature": reading["temperature"],
            "setpoint": reading["setpoint"],
            "setpoint_source": reading["setpoint_source"],
            "manual_setpoint": _number(props.get("handSetPointHeating")),
            "hysteresis": _number(props.get("autoSetPointHysteresis")),
            "head_count": len(head_summary),
            "heads": head_summary,
        })
    zones.sort(key=lambda item: (item["name"].casefold(), item["id"]))

    zone_ids = {item["id"] for item in zones}
    history = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "ClimateZoneTemperatureChangedEvent":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        raw_zone_id = data.get("id")
        if raw_zone_id is None:
            raw_zone_id = data.get("zoneId")
        zone_id = _positive_id(raw_zone_id)
        timestamp = event.get("timestamp")
        old_value, new_value = _number(data.get("oldValue")), _number(data.get("newValue"))
        if (zone_id not in zone_ids or type(timestamp) not in (int, float)
                or timestamp < start or timestamp > stamp
                or old_value is None or new_value is None):
            continue
        history.append({
            "zone_id": zone_id,
            "timestamp": int(timestamp),
            "old": old_value,
            "value": new_value,
            "event_id": _positive_id(event.get("id")),
        })
    history.sort(key=lambda item: (item["timestamp"], item["event_id"] or 0))

    snapshot_at = latest.get("captured_at") if latest else None
    age = max(0, stamp - snapshot_at) if snapshot_at else None
    if snapshot_at is None:
        freshness = "unavailable"
    elif age > 900:
        freshness = "stale"
    else:
        freshness = "fresh"

    return {
        "schema": "thermoflow-fibaro-dashboard/1",
        "source": "shared_sqlite",
        "generated_at": stamp,
        "window": {"start": start, "end": stamp, "days": days},
        "snapshot": {
            "captured_at": snapshot_at,
            "age_seconds": age,
            "freshness": freshness,
            "zone_count": len(zones),
            "head_count": len(heads),
        },
        "zones": zones,
        "history": history,
        "history_count": len(history),
        "collector": collector.status(),
        "read_only": True,
        "safety": {
            "hc3_requests": 0,
            "sqlite_writes": False,
            "device_commands_sent": False,
            "heating_settings_changed": False,
            "historical_head_zone_mapping_inferred": False,
        },
    }
