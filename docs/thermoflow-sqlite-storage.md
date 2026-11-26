# ThermoFlow shared SQLite storage

## Purpose

ThermoFlow's shared local store is a durable analytical layer for selected
source measurements, Fibaro history/snapshots, refill events, weather
forecasts, feature windows, dataset manifests, model metadata/evaluations,
shadow predictions and collector cursors/runs.

**ThingSpeak and SQLite have separate contracts.** ThingSpeak receives only
the explicitly configured channel fields in the existing API writer. SQLite
is not a mirror of ThingSpeak and does not receive every upstream/API field.
It stores curated source observations and derived information required for
history, diagnostics and AI/ML. Model binaries may remain external files;
SQLite stores their paths, hashes, versions and metrics.

## eModul AI/ML input samples

The connected eModul poller stores one compact observation in
`telemetry_samples` per successful poll. Its payload uses
`thermoflow-emodul-ml-input/1` and an explicit allow-list:

- temperatures: CO, DHW, flue, outdoor, room, return and buffer;
- boiler power and its observation timestamp;
- floor, 4D, DHW and circulation pump states;
- 4D valve current/return/set/opening/corrections;
- fuel level/runtime and controller fuel metrics;
- FIRE state/signal/brightness;
- fan, feeder/heater outputs and disinfection state;
- controller mode/status, setpoints and hysteresis.

UI-only tiles, translations, zones, menus, raw controller metadata and
dashboard statistics are excluded. This is a source-observation layer, not a
precomputed training feature set. Versioned feature windows and datasets are
built separately from these observations and registered in their dedicated
SQLite tables.

Each row preserves the selected controller module ID, UTC observation time, local record
time and a SHA-256 digest. Identical replays are ignored; changed content for
the same source timestamp is reported as a conflict and never overwrites the
original sample.

## Runtime storage contract

The production API uses `THERMOFLOW_DB_PATH` to reach the shared persistent
SQLite database. The API poller never creates a missing database or applies
migrations. Schema setup remains an explicit maintenance operation.

This ingestion is fail-soft: missing storage or a storage error does not stop
the live API poller or change existing ThingSpeak writes. No Fibaro or
ThingSpeak collector is enabled by this change.

## Retention

No automatic pruning is enabled. Measure database growth and the useful
training/diagnostic horizon before selecting retention. Preserve raw samples
needed to reproduce feature windows, model datasets and evaluations; use
explicit archival/retention migrations rather than deleting source evidence
implicitly.
