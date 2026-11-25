"""Hourly and daily Fibaro heating-behaviour profile (shadow only).

The profile aggregates verified HC3 event transitions. It deliberately does
not reconstruct state durations: event history does not guarantee an initial
state for the requested window. No polling, persistence or device commands
are performed here.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = "thermoflow-fibaro-time-profile/1"
MAX_PROFILE_BUCKETS = 24 * 7


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return None
    return int(value)


def _new_bucket():
    return {
        "heat_demand_started": 0,
        "heat_demand_stopped": 0,
        "window_opened": 0,
        "window_closed": 0,
        "temperature_events": 0,
        "temperature_clean_samples": 0,
        "temperature_suspected_spikes": 0,
        "temperature_delta_sum_c": 0.0,
        "temperature_abs_delta_sum_c": 0.0,
        "temperature_min_c": None,
        "temperature_max_c": None,
    }


def _add_transition(bucket, transition):
    kind = transition.get("kind")
    if kind == "heatRequired":
        if transition.get("new_value") is True:
            bucket["heat_demand_started"] += 1
        elif transition.get("new_value") is False:
            bucket["heat_demand_stopped"] += 1
    elif kind == "windowOpened":
        if transition.get("new_value") is True:
            bucket["window_opened"] += 1
        elif transition.get("new_value") is False:
            bucket["window_closed"] += 1
    elif kind == "temperature":
        old_value = _number(transition.get("old_value_c"))
        new_value = _number(transition.get("new_value_c"))
        if old_value is None or new_value is None:
            return
        bucket["temperature_events"] += 1
        if transition.get("quality") == "suspected_transient_spike":
            bucket["temperature_suspected_spikes"] += 1
            return
        delta = new_value - old_value
        bucket["temperature_clean_samples"] += 1
        bucket["temperature_delta_sum_c"] += delta
        bucket["temperature_abs_delta_sum_c"] += abs(delta)
        current_min = bucket["temperature_min_c"]
        current_max = bucket["temperature_max_c"]
        bucket["temperature_min_c"] = new_value if current_min is None else min(current_min, new_value)
        bucket["temperature_max_c"] = new_value if current_max is None else max(current_max, new_value)


def _finalize_bucket(bucket):
    result = dict(bucket)
    samples = result.pop("temperature_clean_samples")
    delta_sum = result.pop("temperature_delta_sum_c")
    abs_delta_sum = result.pop("temperature_abs_delta_sum_c")
    result["temperature_mean_delta_c"] = round(delta_sum / samples, 3) if samples else None
    result["temperature_mean_absolute_delta_c"] = round(abs_delta_sum / samples, 3) if samples else None
    return result


def build_fibaro_time_profile(snapshot: dict, event_history: dict, *, timezone_name="Europe/Warsaw") -> dict:
    """Aggregate an analyzed HC3 event-history response into hourly and daily buckets."""
    if not isinstance(snapshot, dict) or not isinstance(event_history, dict):
        raise ValueError("Profil Fibaro wymaga snapshotu i analizy historii HC3")
    if event_history.get("schema") != "thermoflow-fibaro-event-history/1":
        raise ValueError("Nieobsługiwany kontrakt historii Fibaro HC3")
    zones = event_history.get("zones")
    window = event_history.get("window")
    if not isinstance(zones, list) or not isinstance(window, dict):
        raise ValueError("Historia Fibaro nie zawiera stref lub zakresu")
    start = _timestamp(window.get("start"))
    end = _timestamp(window.get("end"))
    if start is None or end is None or start >= end:
        raise ValueError("Nieprawidłowy zakres historii Fibaro")

    try:
        local_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("Nieprawidłowa strefa czasowa profilu Fibaro") from None

    zone_results = []
    global_hourly = defaultdict(_new_bucket)
    global_daily = defaultdict(_new_bucket)
    totals = _new_bucket()
    transitions_seen = 0
    valid_timestamps = set()

    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = zone.get("id")
        zone_name = zone.get("name")
        transitions = zone.get("transitions")
        if not isinstance(transitions, list):
            transitions = []
        zone_hourly = defaultdict(_new_bucket)
        zone_daily = defaultdict(_new_bucket)
        zone_totals = _new_bucket()
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            timestamp = _timestamp(transition.get("timestamp"))
            if timestamp is None or not start <= timestamp <= end:
                continue
            kind = transition.get("kind")
            if kind not in ("heatRequired", "windowOpened", "temperature"):
                continue
            local_time = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(local_zone)
            hour_key = timestamp // 3600
            day_key = local_time.date().isoformat()
            for bucket in (zone_totals, global_hourly[hour_key], global_daily[day_key]):
                _add_transition(bucket, transition)
            _add_transition(zone_hourly[hour_key], transition)
            _add_transition(zone_daily[day_key], transition)
            transitions_seen += 1
            valid_timestamps.add(timestamp)

        hourly = []
        for hour_key, bucket in sorted(zone_hourly.items()):
            utc_start = hour_key * 3600
            local_start = datetime.fromtimestamp(utc_start, timezone.utc).astimezone(local_zone)
            hourly.append({
                "bucket_start": utc_start,
                "bucket_start_at": datetime.fromtimestamp(utc_start, timezone.utc).isoformat(),
                "local_label": local_start.strftime("%Y-%m-%d %H:%M %z"),
                **_finalize_bucket(bucket),
            })
        daily = [
            {"date": day_key, **_finalize_bucket(bucket)}
            for day_key, bucket in sorted(zone_daily.items())
        ]
        zone_results.append({
            "id": zone_id,
            "name": zone_name,
            "totals": _finalize_bucket(zone_totals),
            "hourly": hourly[:MAX_PROFILE_BUCKETS],
            "daily": daily[:7],
            "observed_hour_buckets": len(hourly),
            "observed_days": len(daily),
        })

    # Global totals are accumulated from the same verified transitions, once
    # per transition (not once per zone). A mapped head may appear in multiple
    # zones; event history deliberately fans that event out to each mapped zone.
    for zone in zones:
        if not isinstance(zone, dict) or not isinstance(zone.get("transitions"), list):
            continue
        for transition in zone["transitions"]:
            if not isinstance(transition, dict):
                continue
            timestamp = _timestamp(transition.get("timestamp"))
            if timestamp is None or not start <= timestamp <= end:
                continue
            if transition.get("kind") in ("heatRequired", "windowOpened", "temperature"):
                _add_transition(totals, transition)

    hourly_global = []
    for hour_key, bucket in sorted(global_hourly.items()):
        utc_start = hour_key * 3600
        local_start = datetime.fromtimestamp(utc_start, timezone.utc).astimezone(local_zone)
        hourly_global.append({
            "bucket_start": utc_start,
            "bucket_start_at": datetime.fromtimestamp(utc_start, timezone.utc).isoformat(),
            "local_label": local_start.strftime("%Y-%m-%d %H:%M %z"),
            **_finalize_bucket(bucket),
        })
    daily_global = [
        {"date": day_key, **_finalize_bucket(bucket)}
        for day_key, bucket in sorted(global_daily.items())
    ]

    coverage = event_history.get("coverage") if isinstance(event_history.get("coverage"), dict) else {}
    return {
        "schema": SCHEMA,
        "source": "fibaro-hc3-event-history",
        "mode": "on_demand_read_only",
        "timezone": timezone_name,
        "window": dict(window),
        "totals": _finalize_bucket(totals),
        "counts": {
            "zones": len(zone_results),
            "transitions_aggregated": transitions_seen,
            "distinct_event_timestamps": len(valid_timestamps),
            "observed_hour_buckets": len(hourly_global),
            "observed_days": len(daily_global),
        },
        "hourly": hourly_global[:MAX_PROFILE_BUCKETS],
        "daily": daily_global[:7],
        "zones": zone_results,
        "coverage": {
            "status": coverage.get("status", "unknown"),
            "state_durations_available": False,
            "initial_state_available": False,
            "empty_buckets_synthesized": False,
            "temperature_spikes_excluded_from_clean_means": True,
            "reason": (
                "Only observed event transitions are aggregated. HC3 history does not guarantee "
                "the initial state of the requested window, so on/off durations, occupancy and "
                "unobserved intervals are not reconstructed."
            ),
        },
        "ai_shadow": {
            "training_ready": False,
            "training_ready_reason": (
                "A single on-demand history window is not a longitudinal training dataset. "
                "Persistent, quality-gated history and chronological validation are separate stages."
            ),
            "features_are_observations_not_predictions": True,
        },
        "safety": {
            "read_only": True,
            "hc3_writes": False,
            "background_polling": False,
            "raw_history_persisted": False,
            "device_commands_sent": False,
            "fuel_forecast_applied": False,
            "boiler_pumps_valves_or_setpoints_changed": False,
            "return_protection": "55°C ±1°C",
        },
    }
