# ThermoFlow safe storage maintenance

## Scope

The cleanup utility is deliberately narrow. It may remove only:
- dashboard cache rows whose non-null `expires_at` is at or before the supplied clock;
- collector-run diagnostics older than the configured retention (default 180 days,
  minimum 30 days).

It never deletes telemetry samples, Fibaro events/snapshots, refill events,
weather forecasts, feature windows, datasets, model versions/evaluations,
shadow predictions, or collector cursors.

## Dry run first

```sh
python thermoflow_storage_maintenance.py /path/to/thermoflow.sqlite3
```

This is read-only and prints eligible row counts. The default retention applies
only to `collection_runs`; cache expiry always follows each row's explicit
expiry timestamp.

## Explicit apply

```sh
python thermoflow_storage_maintenance.py /path/to/thermoflow.sqlite3 \
  --apply --backup /path/to/new-backup.sqlite3
```

Apply requires a new backup path and refuses to overwrite an existing file.
The backup is created through SQLite's Online Backup API and integrity-checked
before any deletion. The cleanup then runs in one transaction and performs a
post-cleanup integrity check. No scheduler or startup hook is installed.

Do not point this at the live Raspberry Pi database until the actual Portainer
volume, free space, backup destination, and restore procedure have been
verified. The utility does not run VACUUM or checkpoint WAL; reclaiming file
space is a separate maintenance operation after a verified backup.
