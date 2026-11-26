# ThermoFlow heating analytics and storage

The analytics endpoint reads the v2 Fibaro candidate feature set and existing curated eModul plus ThingSpeak `telemetry_samples`. It does not read raw Fibaro event payloads as an analytics fallback. ThingSpeak field names are retained verbatim (for example `thingspeak:temperatures_power.field1`); field units are not guessed when channel metadata is unavailable.

## Read-only API

`GET /api/fibaro/store/analytics?start=<unix>&end=<unix>`

The endpoint uses the existing Fibaro bearer-token authorization. Optional query parameters are `timezone` (default `Europe/Warsaw`) and `tolerance_seconds` (default 90, maximum 3600). It returns aligned observations, observed-only hourly/daily aggregates, and descriptive diagnostics.

## Explicit aggregate persistence

After reviewing a report, an operator may run:

```bash
docker exec defro-dashboard-api python -m thermoflow_heating_diagnostics \
  --start <unix-start> --end <unix-end> --persist
```

This writes deterministic hourly/daily rows to the existing `feature_windows` table and a manifest to `datasets`, with status `candidate`. Repeated runs with identical content are idempotent. A changed content hash produces a new version. It does not modify raw Fibaro events, telemetry samples, source snapshots, or controller settings.

## Interpretation boundaries

- Head events without historical membership evidence remain head-level and unassigned to a zone.
- `heatRequired` records are transitions, not continuous demand samples.
- eModul and ThingSpeak context are nearest-observation evidence within a bounded time tolerance, not proof of causality.
- ThingSpeak numeric fields are parsed only for descriptive aggregates; original field names are retained and units/meaning are not inferred.
- Missing hours/days and unmatched samples remain missing; no interpolation or forward fill is performed.
- ThingSpeak samples already archived in the shared SQLite database are read directly; this analytics path performs no network requests.
- All diagnostics and datasets remain descriptive/candidate. No model promotion or heating control is performed.
