# ThermoFlow storage audit

## Purpose

The storage audit reports the physical SQLite footprint and the logical payload
volume without changing the database. It is a measurement step before setting
retention, compaction, compression, or a disk quota.

Run against an existing database:

```sh
python thermoflow_storage_diagnostics.py /path/to/thermoflow.sqlite3
```

Payload sizes count UTF-8 bytes, not Unicode characters. The report includes:
- database page size, page count, free-list pages, and an estimate of reclaimable
  space inside the database file;
- sizes of the database, WAL, and SHM files;
- row counts and serialized payload bytes for each shared schema table;
- the largest payload-bearing tables first.

## Safety

This command opens SQLite with `mode=ro`. It does not apply migrations, prune
rows, checkpoint or truncate WAL, run VACUUM, rewrite payloads, or expose row
contents. Run it against a consistent local copy if the database is being
actively written.

## Storage policy decisions

Do not choose a retention period or a hard size limit from schema estimates
alone. First collect real table counts, payload sizes, WAL size, and growth over
time. Keep ThingSpeak as the upstream source of truth. Preserve Fibaro event
IDs and timestamps, and preserve the existing refill normalization, merge, and
void semantics. Model binaries remain external files; SQLite should retain
metadata, hashes, evaluation records, and only the derived data needed for
reproducible training and backtesting.

A later maintenance change should define per-table retention and explicit
checkpoint/VACUUM procedures with backup and restore verification. It must not
silently delete source history or run an unbounded VACUUM on the live Raspberry
Pi volume.
