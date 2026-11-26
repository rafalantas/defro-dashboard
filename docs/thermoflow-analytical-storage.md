# ThermoFlow analytical storage

## Data layers

- `fibaro_events` and `fibaro_snapshots` are the immutable source-evidence layer.
- `feature_windows` with `fibaro_heating_event_v2` is the curated Fibaro analytical layer.
- `datasets` identifies the exact candidate dataset version and content hash.
- `telemetry_samples` contains the explicitly allow-listed eModul measurements.

The analytics reader never falls back from a missing or invalid candidate dataset to raw Fibaro payloads. Raw source events are retained and counted separately; this PR does not prune, rewrite, migrate, or compact production data.

## Read-only operations

```bash
docker exec defro-dashboard-api python -c 'from thermoflow_analytics_store import read_analytics_inventory; import json; print(json.dumps(read_analytics_inventory(), indent=2))'
```

The inventory reports raw archive counts, curated feature-window counts, dataset version/status, telemetry source counts, and SQLite page/free-list counts. It is diagnostic only. It does not run VACUUM, checkpoint, retention, migrations, or data cleanup.

The v2 feature reader accepts a bounded Unix timestamp range and returns only rows that match the expected feature schema and candidate dataset version. No raw payloads are copied into the analytical result.
