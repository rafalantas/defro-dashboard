"""Observed-only hourly and daily ThermoFlow heating aggregates."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "thermoflow-heating-aggregates/1"
METRIC_PATHS = (
    "temperatures.co", "temperatures.return", "temperatures.flue",
    "temperatures.outdoor", "temperatures.dhw", "temperatures.buffer_upper",
    "boiler_power_kw", "valve.current", "valve.return", "valve.opening",
    "pumps.floor", "pumps.dhw", "pumps.circulation", "pumps.valve4d",
    "fan.percent", "fan.rpm",
)


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid timezone name") from None


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _path(data, dotted):
    value = data
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _bucket(stamp, zone, period):
    local = datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone)
    if period == "hour":
        # Include offset in the key so the repeated autumn DST hour stays distinct.
        key = local.strftime("%Y-%m-%dT%H:00:00%z")
        label = local.strftime("%Y-%m-%d %H:00 %z")
    else:
        key = local.date().isoformat()
        label = key
    return key, label


def _new_bucket(key, label):
    return {"bucket": key, "label": label, "observations": 0,
            "matched_fibaro_transitions": 0, "signal_counts": {},
            "metrics": {}, "_timestamps": set(), "_samples": set()}


def _add_metric(target, name, value):
    number = _numeric(value)
    if number is None:
        return
    item = target["metrics"].setdefault(name, {"count": 0, "sum": 0.0,
                                               "min": number, "max": number})
    item["count"] += 1
    item["sum"] += number
    item["min"] = min(item["min"], number)
    item["max"] = max(item["max"], number)


def _finalize(bucket):
    bucket["unique_emodul_samples"] = len(bucket.pop("_samples"))
    timestamps = sorted(bucket.pop("_timestamps"))
    bucket["first_observed_at"] = timestamps[0] if timestamps else None
    bucket["last_observed_at"] = timestamps[-1] if timestamps else None
    for metric in bucket["metrics"].values():
        metric["mean"] = metric["sum"] / metric["count"]
        del metric["sum"]
    return bucket


def aggregate_heating_observations(aligned, *, timezone_name="Europe/Warsaw"):
    """Aggregate only timestamps actually present in the aligned observation set.

    Reused eModul samples are counted once per bucket. Fibaro transitions remain
    event counts; no interpolation, empty buckets, or missing days are created.
    """
    if not isinstance(aligned, dict) or not isinstance(aligned.get("observations"), list):
        raise ValueError("aligned observations are required")
    zone = _zone(timezone_name)
    buckets = {"hour": {}, "day": {}}
    rejected = 0
    for row in aligned["observations"]:
        if not isinstance(row, dict) or not isinstance(row.get("timestamp"), dict):
            rejected += 1
            continue
        stamp = row["timestamp"].get("unix")
        if type(stamp) is not int or stamp <= 0:
            rejected += 1
            continue
        for period in ("hour", "day"):
            key, label = _bucket(stamp, zone, period)
            bucket = buckets[period].setdefault(key, _new_bucket(key, label))
            bucket["observations"] += 1
            signal = row.get("signal")
            if isinstance(signal, str):
                bucket["signal_counts"][signal] = bucket["signal_counts"].get(signal, 0) + 1
            if row.get("matched") and isinstance(row.get("emodul"), dict):
                bucket["matched_fibaro_transitions"] += 1
                emodul = row["emodul"]
                observed = emodul.get("observed_at", {})
                sample_stamp = observed.get("unix") if isinstance(observed, dict) else None
                sample_key = (sample_stamp, emodul.get("source_key"))
                is_new_sample = sample_stamp is not None and sample_key not in bucket["_samples"]
                if is_new_sample:
                    bucket["_samples"].add(sample_key)
                    bucket["_timestamps"].add(sample_stamp)
                    data = emodul.get("data")
                    if isinstance(data, dict):
                        for path in METRIC_PATHS:
                            _add_metric(bucket, path, _path(data, path))
    result = {}
    for period, values in buckets.items():
        result["hourly" if period == "hour" else "daily"] = [
            _finalize(values[key]) for key in sorted(values)
        ]
    return {
        "schema": SCHEMA,
        "status": "available" if result["hourly"] or result["daily"] else "collecting",
        "timezone": timezone_name,
        "hourly": result["hourly"],
        "daily": result["daily"],
        "coverage": {
            "input_transitions": len(aligned["observations"]),
            "rejected_rows": rejected,
            "observed_hours": len(result["hourly"]),
            "observed_days": len(result["daily"]),
            "empty_buckets_synthesized": 0,
        },
        "semantics": {
            "source_samples_deduplicated_within_bucket": True,
            "missing_hours_or_days_filled": False,
            "interpolation": False,
            "telemetry_is_causal_evidence": False,
        },
    }
