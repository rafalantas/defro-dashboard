# Fibaro shadow pipeline CLI

The pipeline reads only the shared SQLite database. It does not contact HC3 or
start a collector.

## Dry run (default)

```sh
python -m thermoflow_fibaro_shadow_pipeline /path/to/thermoflow.sqlite3 \
  --start 1794000000 --end 1800000000
```

It reports quality gates, observed daily features, shadow model status, and
chronological backtest. No rows are written.

## Persist evaluated artifacts

```sh
python -m thermoflow_fibaro_shadow_pipeline /path/to/thermoflow.sqlite3 \
  --start 1794000000 --end 1800000000 --persist
```

Persistence occurs only when the pipeline has ready quality evidence, an
evaluated model, and a completed backtest. The CLI writes compact derived
artifacts to the existing shared schema; raw event history is not copied.
Use `--recorded-at` for reproducible/idempotent reruns.

The database must already have migrations applied and contain persisted Fibaro
events plus at least one snapshot. This CLI does not initialize or migrate a
database, fetch data, schedule collection, or modify any device.
