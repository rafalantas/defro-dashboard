"""Deterministic, read-only replay harness for timestamped ThermoFlow history."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from fuel_telemetry_diagnostics import build_raw_diagnostics
from thermoflow_diagnostic_intelligence import build_thermoflow_intelligence


def replay_telemetry(
    points: Mapping[str, Sequence[Sequence[object]]],
    *,
    now: datetime | None = None,
    cycles: dict | None = None,
) -> dict:
    """Run the production diagnostic stages against an immutable history snapshot.

    The function deliberately performs no fetching, interpolation, writes, or
    control actions. Callers can persist the returned JSON as a replay artifact.
    """
    timestamp = now or datetime.now(timezone.utc)
    # The intelligence pipeline consumes Unix timestamps, while the raw
    # diagnostics provider consumes timezone-aware datetimes. Adapt only the
    # replay copy; never mutate the caller's snapshot or synthesize samples.
    raw = {}
    for key, values in points.items():
        converted = []
        for sample in values:
            row = list(sample)
            if len(row) >= 2 and isinstance(row[0], (int, float)):
                row[0] = datetime.fromtimestamp(row[0], timezone.utc)
            converted.append(row)
        raw[key] = converted

    intelligence_raw = {
        key: [[int(sample[0].timestamp()) if isinstance(sample[0], datetime) else sample[0],
               *sample[1:]] for sample in values]
        for key, values in raw.items()
    }
    raw_result = build_raw_diagnostics(raw, timestamp)
    intelligence = build_thermoflow_intelligence(intelligence_raw, cycles or {})
    return {
        "replay_version": 1,
        "generated_at": timestamp.isoformat(),
        "input_series": sorted(raw),
        "input_samples": sum(len(values) for values in raw.values()),
        "raw_diagnostics": raw_result,
        "thermoflow_intelligence": intelligence,
        "safety": "read_only_replay; no interpolation; no fuel estimate or boiler control changes",
    }
