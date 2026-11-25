# Fibaro history on the shared ThermoFlow SQLite store

The HC3 history collector now writes into the common `thermoflow.sqlite3`
database using the schema introduced by migration `001_initial.sql`.

## Contract

- Collection remains explicitly pull-driven; there is no timer, worker, or
  automatic polling.
- The only upstream call is `provider.get_events_range(start, end)` (GET).
- Requested windows are ordered and limited to seven days.
- Event IDs are idempotency keys. Repeated identical payloads count as
  duplicates; a reused ID with a different payload is rejected and marks the
  run partial. Existing data is never overwritten.
- Events, collection-run metrics, and the monotonic collector cursor are
  committed in one transaction.
- Partial runs do not advance the cursor.
- Retention is explicit through `prune()` and deletes only from
  `fibaro_events`; default retention is 90 days.
- No Fibaro credentials are persisted.

## Migration prerequisite

The database must already have the shared schema applied using
`SQLiteStore().migrate()`. The collector fails closed if required tables are
missing. It never creates a second database or runs migrations itself.

## Compatibility and safety

This module is not wired into API startup or a background scheduler in this
step. Existing Fibaro API snapshots and history reads are unchanged. The
collector is a storage adapter for a later, explicitly controlled integration.
No commands are sent to HC3 and no heating settings or control paths change.

## Tests

```sh
python -m unittest discover -s tests -p test_thermoflow_fibaro_shared_collector.py -v
```
