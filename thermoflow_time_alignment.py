"""Explicit UTC/local-time alignment for Fibaro and eModul observations.

Nearest-observation matching only: no interpolation, forward fill, or synthetic
samples. Missing matches remain explicitly unmatched.
"""
from __future__ import annotations
import bisect
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "thermoflow-time-alignment/2"

def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid timezone name") from None

def normalize_timestamp(value):
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

def timestamp_labels(stamp, timezone_name="Europe/Warsaw"):
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("timestamp must be positive Unix seconds")
    local = datetime.fromtimestamp(stamp, timezone.utc).astimezone(_zone(timezone_name))
    utc = datetime.fromtimestamp(stamp, timezone.utc)
    return {"unix": stamp, "utc": utc.isoformat().replace("+00:00", "Z"),
            "local": local.isoformat(), "local_date": local.date().isoformat(),
            "local_hour": local.strftime("%Y-%m-%dT%H:00:00%z"),
            "timezone": timezone_name,
            "utc_offset_seconds": int(local.utcoffset().total_seconds())}

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
    if isinstance(data, dict):
        return data
    # ThingSpeak telemetry is archived as compact field1..field8 feed payloads.
    fields = {key: item for key, item in value.items()
              if isinstance(key, str) and key.startswith("field") and item is not None}
    return fields or None

def align_observations(fibaro_features, telemetry_samples, *,
                       tolerance_seconds=90, timezone_name="Europe/Warsaw"):
    """Align Fibaro transitions to real telemetry, one sample per event/source.

    A sample may provide context to several signals emitted by the same Fibaro
    source event, but is never reused for a different event from that source.
    No interpolation, forward fill, or synthetic observations are created.
    """
    if type(tolerance_seconds) is not int or not 0 <= tolerance_seconds <= 3600:
        raise ValueError("tolerance_seconds must be between 0 and 3600")
    _zone(timezone_name)

    samples_by_source = {}
    rejected_samples = 0
    for sample in telemetry_samples:
        if not isinstance(sample, dict):
            rejected_samples += 1
            continue
        source = sample.get("source") or "emodul"
        if not isinstance(source, str) or not source:
            rejected_samples += 1
            continue
        try:
            stamp = normalize_timestamp(sample.get("observed_at"))
        except ValueError:
            rejected_samples += 1
            continue
        data = _payload(sample)
        if data is None:
            rejected_samples += 1
            continue
        samples_by_source.setdefault(source, []).append(
            {"stamp": stamp, "data": data, "source_key": sample.get("source_key")}
        )
    for source in samples_by_source:
        samples_by_source[source].sort(
            key=lambda item: (item["stamp"], str(item["source_key"] or ""))
        )

    events, rejected_features = [], 0
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
        for transition in transitions:
            if not isinstance(transition, dict):
                rejected_features += 1
                continue
            source_event_id = transition.get("source_event_id")
            event_key = (event_stamp, source_event_id if source_event_id is not None
                         else ("timestamp", event_stamp))
            events.append({
                "event_key": event_key, "stamp": event_stamp,
                "transition": transition,
            })
    events.sort(key=lambda item: (item["stamp"], str(item["event_key"][1])))

    # Allocate nearest samples independently for each source. Greedy nearest
    # allocation in chronological event order is deterministic and prevents
    # the previous many-events-to-one-sample overcount.
    assigned = {}
    for source, source_samples in samples_by_source.items():
        stamps = [item["stamp"] for item in source_samples]
        used = set()
        for event in events:
            key = event["event_key"]
            if key in assigned and source in assigned[key]:
                continue
            event_stamp = event["stamp"]
            pos = bisect.bisect_left(stamps, event_stamp)
            left, right, candidates = pos - 1, pos, []
            while left >= 0 or right < len(source_samples):
                left_delta = abs(source_samples[left]["stamp"] - event_stamp) if left >= 0 else None
                right_delta = abs(source_samples[right]["stamp"] - event_stamp) if right < len(source_samples) else None
                next_delta = min(value for value in (left_delta, right_delta) if value is not None)
                if next_delta > tolerance_seconds:
                    break
                if left_delta is not None and left_delta == next_delta:
                    if left not in used:
                        candidates.append(left)
                    left -= 1
                if right_delta is not None and right_delta == next_delta:
                    if right not in used:
                        candidates.append(right)
                    right += 1
                if candidates:
                    break
            if not candidates:
                continue
            selected = min(candidates, key=lambda index: (
                abs(source_samples[index]["stamp"] - event_stamp),
                source_samples[index]["stamp"],
                str(source_samples[index]["source_key"] or ""),
            ))
            used.add(selected)
            item = source_samples[selected]
            assigned.setdefault(key, {})[source] = {
                "observed_at": timestamp_labels(item["stamp"], timezone_name),
                "source_key": item["source_key"],
                "source": source,
                "delta_seconds": item["stamp"] - event_stamp,
                "data": item["data"],
            }

    aligned = []
    for event in events:
        transition = event["transition"]
        telemetry = assigned.get(event["event_key"], {})
        emodul = telemetry.get("emodul")
        aligned.append({
            "timestamp": timestamp_labels(event["stamp"], timezone_name),
            "source_event_id": transition.get("source_event_id"),
            "signal": transition.get("signal"), "entity": transition.get("entity"),
            "device_id": transition.get("device_id"), "zone_id": transition.get("zone_id"),
            "historical_zone_mapping": transition.get("historical_zone_mapping"),
            "old_value": transition.get("old_value"), "new_value": transition.get("new_value"),
            "emodul": emodul,
            "telemetry": telemetry,
            "matched": bool(telemetry),
            "matched_sources": sorted(telemetry),
        })
    matched_by_source = {
        source: sum(source in assigned.get(event["event_key"], {}) for event in events)
        for source in sorted(samples_by_source)
    }
    total = len(aligned)
    matched = sum(bool(row["matched"]) for row in aligned)
    return {
        "schema": SCHEMA, "status": "available" if aligned else "collecting",
        "timezone": timezone_name, "tolerance_seconds": tolerance_seconds,
        "counts": {
            "fibaro_transitions": total, "matched": matched,
            "unmatched": total - matched,
            "matched_by_source": matched_by_source,
            "rejected_features": rejected_features,
            "rejected_telemetry_samples": rejected_samples,
            "rejected_emodul_samples": rejected_samples,
        },
        "observations": aligned,
        "semantics": {
            "matching": "nearest_real_observation_one_to_one_per_source_event",
            "sample_reuse_across_distinct_events": False,
            "interpolation": False, "forward_fill": False,
            "missing_samples_synthesized": False,
            "causal_relationship_inferred": False,
        },
    }
