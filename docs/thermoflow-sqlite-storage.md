# ThermoFlow shared SQLite storage

## Purpose

ThermoFlow's shared local store is a durable analytical layer for selected
source samples, Fibaro history/snapshots, refill events, weather forecasts,
feature windows, dataset manifests, model metadata/evaluations, shadow
predictions, collector cursors/runs, and dashboard cache entries.

ThingSpeak and the upstream devices remain source systems. This database is
not intended to blindly mirror every upstream record. Model binaries remain
external files; the database stores their paths, hashes, versions, and metrics.

## First-step safety boundary

This change adds the schema and migration engine only. It does not:
- open the database during API import or startup;
- start collectors or background workers;
- redirect existing JSON ledgers, ThingSpeak reads, or Fibaro history;
- change dashboard response paths, heater control, or any live device setting.

The store is initialized only by an explicit `SQLiteStore(...).migrate()`
call from a future integration step.

## Location and configuration

Set `THERMOFLOW_DB_PATH` to the persistent SQLite file path. The default is
`/var/lib/thermoflow/thermoflow.sqlite3`. The API image includes the SQL
migration files under `/app/db/migrations`; this PR intentionally does not
choose or mount a production host directory. Confirm the actual Portainer
stack and storage target before deployment.

## Migration contract

- SQL files are immutable once applied and use `NNN_description.sql` names.
- `schema_migrations` records version, filename, SHA-256, and application time.
- Each migration and its ledger row run in one `BEGIN IMMEDIATE` transaction.
- Existing migration checksums are verified; changed or missing applied
  migrations fail closed.
- Unknown database migration versions fail closed rather than silently
  downgrading.
- SQLite uses WAL, foreign keys, a bounded busy timeout, and NORMAL sync mode.
- Use SQLite's online backup API through `backup_to()`; do not copy a live
  database file while its WAL may contain committed data.

## Schema v1

The initial schema is deliberately generic and source-aware. It separates raw
observations from derived feature windows and model outputs. Unique keys and
indexes support idempotent ingestion and time-range reads. Follow-up migrations
will add source-specific constraints only when their contracts are integrated
and tested.

## Validation

Run:

```sh
python -m unittest tests.test_thermoflow_store -v
```
