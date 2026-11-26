"""Explicit UTC/local-time alignment for Fibaro and eModul observations.

This module performs nearest-observation matching only. It never interpolates,
forward-fills, invents samples, or treats a missing match as a zero value.
"""
from __future__ import annotations

import bisect
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "thermoflow-time-alignment/1"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid timezone name") from None


def normalize_timestamp(value) -> int:
    """Accept positive Unix seconds or an ISO-8601 timestamp with an offset."""
    if type(value) is int:
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("timestamp must be Unix seconds or ISO-8601") from None
        if parsed.utcoffset() is None:
            raise ValueError("ISO-8601 timestamp must include a timezone offset")
        stamp = int(parsed.timestamp())
    else:
        raise ValueError("timestamp must be Unix seconds or ISO-8601")
    if stamp <= 0:
        raise ValueError("timestamp must be positive")
    return stamp


def timestamp_labels(stamp: int, timezone_name="Europe/Warsaw") -> dict:
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("timestamp must be positive Unix seconds")
    zone = _zone(timezone_name)
    utc = datetime.fromtimestamp(stamp, timezone.utc)
    local = utc.astimezone(zone)
    return {
        "unix": stamp,
        "utc": utc.isoformat().replace("+00:00", "Z"),
        "local": local.isoformat(),
        "local_date": local.date().isoformat(),
        "local_hour": local.strftime("%Y-%m-%dT%H:00:00%z"),
        "timezone": timezone_name,
        "utc_offset_seconds": int(local.utcoffset().total_seconds()),
    }


def _payload(sample):
    value = sample.get("payload")
    if value is None:
        value = sample.get("payload_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    return data if isinstance(data, dict) else None


def align_observations(fibaro_features, emodul_samples, *,
                       tolerance_seconds=90, timezone_name="Europe/Warsaw") -> dict:
    """Attach the nearest real eModul sample to each curated Fibaro transition."""
    if type(tolerance_seconds) is not int or not 0 <= tolerance_seconds <= 3600:
        raise ValueError("tolerance_seconds must be between 0 and 3600")
    zone = _zone(timezone_name)
    samples = []
    rejected_samples = 0
    for sample in emodul_samples:
        try:
            stamp = normalize_timestamp(sample.get("observed_at"))
        except (AttributeError, ValueError):
            rejected_samples += 1
            continue
        data = _payload(sample)
        if data is None:
            rejected_samples += 1
            continue
        samples.append((stamp, data, sample.get("source_key")))
    samples.sort(key=lambda item: item[0])
    stamps = [item[0] for item in samples]

    aligned = []
    rejected_features = 0
    for feature in fibaro_features:
        if not isinstance(feature, dict):
            rejected_features += 1
            continue
        values = feature.get("values")
        if values is None and isinstance(feature.get("values_json"), str):
            try:
                values = json.loads(feature["values_json"])
            except ValueError:
                rejected_features += 1
                continue
        if not isinstance(values, dict) or values.get("schema") != "thermoflow-fibaro-heating-feature/2":
            rejected_features += 1
            continue
        try:
            event_stamp = normalize_timestamp(values.get("observed_at"))
        except ValueError:
            rejected_features += 1
            continue
        transitions = values.get("transitions")
        if not isinstance(transitions, list):
            rejected_features += 1
            continue

        pos = bisect.bisect_left(stamps, event_stamp)
        candidates = []
        if pos < len(samples):
            candidates.append(samples[pos])
        if pos:
            candidates.append(samples[pos - 1])
        nearest = min(candidates, key=lambda item: (abs(item[0] - event_stamp), item[0])) if candidates else None
        matched = nearest is not None and abs(nearest[0] - event_stamp) <= tolerance_seconds
        context = nearest[1] if matched else None
        for transition in transitions:
            if not isinstance(transition, dict):
                rejected_features += 1
                continue
            aligned.append({
                "timestamp": timestamp_labels(event_stamp, timezone_name),
                "source_event_id": transition.get("source_event_id"),
                "signal": transition.get("signal"),
                "entity": transition.get("entity"),
                "device_id": transition.get("device_id"),
                "zone_id": transition.get("zone_id"),
                "historical_zone_mapping": transition.get("historical_zone_mapping"),
                "old_value": transition.get("old_value"),
                "new_value": transition.get("new_value"),
                "emodul": ({
                    "observed_at": timestamp_labels(nearest[0], timezone_name),
                    "source_key": nearest[2],
                    "delta_seconds": nearest[0] - event_stamp,
                    "data": context,
                } if matched else None),
                "matched": bool(matched),
            })

    aligned.sort(key=lambda row: (row["timestamp"]["unix"],
                                 row["source_event_id"] or 0))
    return {
        "schema": SCHEMA,
        "status": "available" if aligned else "collecting",
        "timezone": timezone_name,
        "tolerance_seconds": tolerance_seconds,
        "counts": {
            "fibaro_transitions": len(aligned),
            "matched": sum(row["matched"] for row in aligned),
            "unmatched": sum(not row["matched"] for row in aligned),
            "rejected_features": rejected_features,
            "rejected_emodul_samples": rejected_samples,
        },
        "observations": aligned,
        "semantics": {
            "matching": "nearest_real_observation",
            "interpolation": False,
            "forward_fill": False,
            "missing_samples_synthesized": False,
            "causal_relationship_inferred": False,
        },
    }
