# ThermoFlow shared SQLite storage

## Purpose

ThermoFlow's shared local store is a durable analytical layer for selected
source samples, Fibaro history/snapshots, refill events, weather forecasts,
feature windows, dataset manifests, model metadata/evaluations, shadow
predictions, collector cursors/runs, and dashboard cache entries.

ThingSpeak and the upstream devices remain source systems. This database is
not intended to blindly mirror every upstream record. Model binaries remain
external files; the database stores their paths, hashes, versions, and metrics.

## Runtime storage contract

The production Compose stack configures:

- `THERMOFLOW_DB_PATH=/var/lib/thermoflow/thermoflow.sqlite3`;
- the named Docker volume `defro-dashboard-thermoflow-db`, mounted at
  `/var/lib/thermoflow` in the API container;
- the existing `./data/thermoflow:/run/thermoflow:ro` mount for read-only
  model/configuration files.

The named volume survives API container replacement and image updates. It is
separate from the read-only model-file mount. Do not remove the volume as part
of routine container or stack updates. Removing the named volume permanently
deletes its database contents unless a verified backup exists.

The database path is fixed in the production Compose file so it cannot
silently point outside the persistent mount. For local scripts and tests,
`SQLiteStore` still accepts an explicit path or the `THERMOFLOW_DB_PATH`
environment variable.

## First-step safety boundary

This runtime wiring does not:

- open the database during API import or startup;
- run schema migrations automatically;
- start collectors or background workers;
- redirect existing JSON ledgers, ThingSpeak reads, or Fibaro history;
- change dashboard response paths, heater control, or any live device setting.

The store is initialized only by an explicit `SQLiteStore(...).migrate()`
call from an authorized maintenance or integration workflow. The first
production initialization must be a separately planned operation with the
actual Portainer stack confirmed, a verified backup/restore path, and explicit
operator approval. This PR only provisions durable storage and points the
store configuration at it.

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
python -m unittest discover -s tests -p 'test_thermoflow_store.py' -v
python -m unittest discover -s tests -p 'test_thermoflow_compose_storage.py' -v
```


## eModul live snapshot ingestion

The API poller now archives each connected, normalized eModul snapshot in
`telemetry_samples` independently of dashboard requests. The source is
`emodul`; the source key combines the configured module ID and the exact
upstream snapshot timestamp. `observed_at` is the UTC epoch parsed from
`fetched_at`, while `recorded_at` records when this API process wrote the row.
The full normalized snapshot is stored as canonical JSON with a SHA-256 digest.

This is an additional local archive; it does not replace the existing
ThingSpeak writes or reads. Identical replays are ignored, and a changed payload
for the same module/timestamp is reported as a conflict rather than overwriting
the original evidence. The API poller does not create a missing database, run
migrations, or start any collector. If the database or schema is not ready,
archiving is skipped and live API polling continues normally.

The first ingestion step intentionally has no automatic pruning. Monitor the
SSD database growth before selecting a retention period; do not delete source
history or old database volumes as part of this change. This poller stores the
normalized eModul snapshot (including its current tiles, zones, menus and
statistics projection), not raw HTTP response bodies from the eModul service.
