"""Read-only audit and daily features for the two ThermoFlow ThingSpeak channels.

Field numbers are kept in sync with api.py: THINGSPEAK_COMPACT_CHANNEL_FIELDS.
No credentials or raw feed payloads are returned by this module.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from fuel_schema import telemetry_channels, timezone
from fuel_telemetry_diagnostics import build_raw_diagnostics

TZ = timezone()
MAX_SAMPLE_GAP_SECONDS = 300
CHANNELS = telemetry_channels()
FIRE_SERIES = (
    ("fire_detected", "Obecność płomienia", "thermoflow-fire-detected", "state"),
    ("fire_brightness", "Jasność płomienia", "thermoflow-fire-brightness", "value"),
)

def _timestamp(raw):
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def _number(raw):
    try:
        value = float(raw)
        return value if value == value and abs(value) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 1)


def analyze(feeds_by_channel, channel_ids, days, now=None, ignition_config=None):
    """Return coverage audit and day aggregates; optional now makes date windows deterministic."""
    points = defaultdict(list)
    channel_audit = []
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    start = (now - timedelta(days=days)).date()

    for channel_index, schema in enumerate(CHANNELS):
        feeds = feeds_by_channel[channel_index] if channel_index < len(feeds_by_channel) else []
        parsed_count = 0
        channel_times = []
        for feed in feeds:
            ts = _timestamp(feed.get("created_at"))
            if ts is None:
                continue
            parsed_count += 1
            channel_times.append(ts)
            for field_index, (key, _label, _series_id, kind) in enumerate(schema["series"], 1):
                value = _number(feed.get(f"field{field_index}"))
                if value is not None:
                    points[key].append((ts, value))
        channel_audit.append({
            "name": schema["name"],
            "configured": bool(channel_ids[channel_index]) if channel_index < len(channel_ids) else False,
            "feed_count": len(feeds),
            "timestamped_feeds": parsed_count,
            "first_sample": min(channel_times).isoformat() if channel_times else None,
            "last_sample": max(channel_times).isoformat() if channel_times else None,
        })

    fire_feeds = feeds_by_channel[2] if len(feeds_by_channel) > 2 else []
    fire_times = []
    for feed in fire_feeds:
        ts = _timestamp(feed.get("created_at"))
        if ts is None:
            continue
        fire_times.append(ts)
        for index, (key, _label, _series_id, _kind) in enumerate(FIRE_SERIES, 1):
            value = _number(feed.get(f"field{index}"))
            if value is not None:
                points[key].append((ts, value))
    if len(channel_ids) > 2:
        channel_audit.append({
            "name": "fire_sensor",
            "configured": bool(channel_ids[2]),
            "feed_count": len(fire_feeds),
            "timestamped_feeds": len(fire_times),
            "first_sample": min(fire_times).isoformat() if fire_times else None,
            "last_sample": max(fire_times).isoformat() if fire_times else None,
        })

    # CO temperature history already lives in the primary ThingSpeak channel.
    # Current setpoint/hysteresis come from the live eModul API snapshot; they
    # are applied as a current-config assumption, never presented as historical
    # settings. Missing live values leave ignition requests unclassified.
    co_samples = list(points.get("co_c", []))
    if co_samples:
        points["boiler_temp_c"] = co_samples
    ignition_config = ignition_config or {}
    target = _number(ignition_config.get("setpoint_c"))
    band = _number(ignition_config.get("hysteresis_c"))
    if target is not None and band is not None:
        points["boiler_setpoint_c"] = [(ts, target) for ts, _ in co_samples]
        points["boiler_hysteresis_c"] = [(ts, band) for ts, _ in co_samples]
    raw_diagnostics = build_raw_diagnostics(points, now)
    raw_diagnostics["ignition_activity"]["configuration"] = {
        "source": ignition_config.get("source", "live_emodul_snapshot"),
        "setpoint_c": target,
        "hysteresis_c": band,
        "applied_to_historical_co_samples": bool(co_samples and target is not None and band is not None),
        "historical_setting_changes_known": False,
        "historical_settings_assumption": "constant_current_values",
        "captured_at": ignition_config.get("captured_at"),
    }
    series_audit = []
    daily_values = defaultdict(lambda: defaultdict(list))
    daily_states = defaultdict(lambda: defaultdict(list))
    for schema in CHANNELS:
        for key, label, series_id, kind in schema["series"]:
            values = sorted(points.get(key, []))
            gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(values, values[1:])]
            series_audit.append({
                "id": series_id, "key": key, "label": label, "kind": kind,
                "samples": len(values),
                "first_sample": values[0][0].isoformat() if values else None,
                "last_sample": values[-1][0].isoformat() if values else None,
                "median_interval_s": round(median(gaps), 1) if gaps else None,
                "p95_interval_s": _percentile(gaps, 0.95),
                "gaps_over_5m": sum(gap > MAX_SAMPLE_GAP_SECONDS for gap in gaps),
                "missing": not bool(values),
            })
            for ts, value in values:
                day = ts.date().isoformat()
                if ts.date() >= start:
                    if kind == "state" and key != "pump_valve4d":
                        # Pompa 4D pracuje stale; nie raportujemy jej czasu pracy.
                        daily_states[day][key].append((ts, value >= 0.5))
                    elif key == "room_c" and value <= 0:
                        # Zachowujemy surową próbkę w audycie serii, ale nie
                        # pozwalamy, by brak łączności (np. 0°C) zaniżał agregaty.
                        daily_states[day]["room_c_invalid"].append((ts, False))
                    else:
                        daily_values[day][key].append(value)

    for key, label, series_id, kind in FIRE_SERIES:
        values = sorted(points.get(key, []))
        gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(values, values[1:])]
        series_audit.append({
            "id": series_id, "key": key, "label": label, "kind": kind,
            "samples": len(values),
            "first_sample": values[0][0].isoformat() if values else None,
            "last_sample": values[-1][0].isoformat() if values else None,
            "median_interval_s": round(median(gaps), 1) if gaps else None,
            "p95_interval_s": _percentile(gaps, 0.95),
            "gaps_over_5m": sum(gap > MAX_SAMPLE_GAP_SECONDS for gap in gaps),
            "missing": not bool(values),
        })

    daily = []
    all_keys = [item[0] for channel in CHANNELS for item in channel["series"] if item[0] != "pump_valve4d"]
    for offset in range(days, -1, -1):
        day = (now.date() - timedelta(days=offset)).isoformat()
        row = {"day": day}
        for key in all_keys:
            vals = daily_values[day].get(key, [])
            if vals:
                row[key + "_mean"] = round(sum(vals) / len(vals), 3)
                row[key + "_min"] = round(min(vals), 3)
                row[key + "_max"] = round(max(vals), 3)
                row[key + "_samples"] = len(vals)
            else:
                row[key + "_mean"] = None
                row[key + "_samples"] = 0
        invalid_room = len(daily_states[day].get("room_c_invalid", []))
        row["room_c_invalid_samples"] = invalid_room
        for key, samples in daily_states[day].items():
            if key == "room_c_invalid":
                continue
            samples.sort()
            known_seconds = on_seconds = 0.0
            transitions = 0
            previous = None
            for (t0, state), (t1, _next_state) in zip(samples, samples[1:]):
                gap = (t1 - t0).total_seconds()
                if 0 < gap <= MAX_SAMPLE_GAP_SECONDS:
                    known_seconds += gap
                    if state:
                        on_seconds += gap
                if previous is not None and state != previous:
                    transitions += 1
                previous = state
            row[key + "_runtime_h"] = round(on_seconds / 3600, 2)
            row[key + "_known_h"] = round(known_seconds / 3600, 2)
            row[key + "_transitions"] = transitions
            row[key + "_samples"] = len(samples)
        daily.append(row)

    return {
        "mode": "read_only_audit",
        "timezone": str(TZ),
        "requested_days": days,
        "channels": channel_audit,
        "series": series_audit,
        "daily": daily,
        "raw_diagnostics": raw_diagnostics,
        "quality": {
            "series_count": len(series_audit),
            "available_series": sum(not item["missing"] for item in series_audit),
            "empty_series": [item["id"] for item in series_audit if item["missing"]],
            "max_state_gap_seconds": MAX_SAMPLE_GAP_SECONDS,
        },
        "limitations": [
            "Stan pomp jest liczony tylko między próbkami oddalonymi maksymalnie o 5 minut.",
            "Niepełne pokrycie doby i brakujące próbki pozostają jawne; nie są interpolowane.",
            "Agregaty telemetryczne nie są jeszcze wejściem do modelu zużycia paliwa.",
        ],
    }
