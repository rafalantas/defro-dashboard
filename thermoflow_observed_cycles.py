"""Offline import of measured ThermoFlow FIRE cycles; no network or control."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from thermoflow_diagnostic_intelligence import segment_burner_cycles
from thermoflow_ml_burner_adapter import FEATURE_KEYS, TARGET_KEY
from thermoflow_ml_dataset import prepare_chronological_dataset

INPUT_SCHEMA = "thermoflow-raw-fire/1"
REPORT_SCHEMA = "thermoflow-observed-cycles/1"
FIRE = "thermoflow-fire"
SIGNALS = (FIRE, "thermoflow-flue", "thermoflow-boiler-power")
MAX_UTC_SECONDS = 253402300799


def _timestamp(value):
    return type(value) is int and 0 < value <= MAX_UTC_SECONDS


def _signal_points(values, *, binary):
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("each signal must contain [utc_epoch_seconds, value] samples")
    counts, candidates, invalid_at = Counter(), [], set()
    rejected = Counter()
    for point in values:
        if not isinstance(point, (list, tuple)) or not point or not _timestamp(point[0]):
            rejected["invalid_timestamp"] += 1
            continue
        ts = point[0]
        counts[ts] += 1
        if len(point) != 2:
            invalid_at.add(ts)
            rejected["invalid_value"] += 1
            continue
        value = point[1]
        try:
            valid = type(value) in (int, float) and isfinite(value)
        except OverflowError:
            valid = False
        if not valid or (binary and value not in (0, 1)):
            invalid_at.add(ts)
            rejected["invalid_value"] += 1
            continue
        candidates.append((ts, value))
    duplicates = {ts for ts, count in counts.items() if count > 1}
    rejected["duplicate_timestamp_samples"] = sum(counts[ts] for ts in duplicates)
    return (sorted(point for point in candidates if point[0] not in duplicates),
            invalid_at | duplicates, dict(rejected))


def _epoch(iso):
    return int(datetime.fromisoformat(iso).timestamp())


def build_observed_cycle_dataset(snapshot: Mapping) -> dict:
    """Extract complete measured cycles and avoid training across unknown idle time."""
    if (not isinstance(snapshot, Mapping) or snapshot.get("schema") != INPUT_SCHEMA
            or snapshot.get("sampling") != "raw"):
        raise ValueError(f"expected schema {INPUT_SCHEMA} and sampling='raw'")
    points = snapshot.get("points")
    if not isinstance(points, Mapping) or FIRE not in points:
        raise ValueError(f"points must include {FIRE} (use an empty array if unavailable)")
    clean, quality = {}, {}
    barriers = set()
    for signal in SIGNALS:
        clean[signal], invalid, quality[signal] = _signal_points(
            points.get(signal, []), binary=signal == FIRE)
        if signal == FIRE:
            barriers = invalid

    report = segment_burner_cycles(
        clean, report_limit=None, include_segments=True, break_timestamps=barriers)
    events = sorted(report["cycles"] + report["incomplete_cycles"],
                    key=lambda item: (item["segment"], item["start"]))
    previous = None
    rows = []
    unpaired_complete = 0
    for event in events:
        if not event["complete"]:
            previous = None
            continue
        start, end = _epoch(event["start"]), _epoch(event["end"])
        if previous is not None and previous[0] == event["segment"]:
            _, previous_end, previous_duration = previous
            rows.append({
                "timestamp": start, "complete": True,
                "previous_duration_minutes": previous_duration,
                "idle_minutes": (start - previous_end) / 60,
                TARGET_KEY: (end - start) / 60,
            })
        else:
            unpaired_complete += 1
        previous = event["segment"], end, (end - start) / 60

    dataset = prepare_chronological_dataset(
        rows, feature_keys=FEATURE_KEYS, target_key=TARGET_KEY)
    fire = clean[FIRE]
    return {
        "schema": REPORT_SCHEMA,
        "source_schema": INPUT_SCHEMA,
        "mode": "offline_read_only_no_training",
        "first_fire_timestamp": fire[0][0] if fire else None,
        "last_fire_timestamp": fire[-1][0] if fire else None,
        "input_quality": quality,
        "cycles": report["cycles"],
        "incomplete_cycles": report["incomplete_cycles"],
        "counts": {
            "complete": report["complete"],
            "incomplete": report["incomplete"],
            "segments": report.get("timeline_segments", 0),
            "ignored_transitions": report["ignored_transitions"],
            "complete_without_observed_previous_cycle": unpaired_complete,
        },
        "dataset": dataset,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="local raw snapshot JSON")
    args = parser.parse_args(argv)
    try:
        data = args.snapshot.read_bytes()
        result = build_observed_cycle_dataset(json.loads(data))
        result["input_sha256"] = hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as error:
        print(f"Snapshot processing failed: {error}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, allow_nan=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
