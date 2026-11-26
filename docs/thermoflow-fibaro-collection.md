# Explicit Fibaro HC3 collection

The thermoflow_fibaro_collection module provides one-shot ingestion and a resumable full-history backfill for the shared ThermoFlow SQLite database. The dedicated scheduler performs the configured backfill range first, in bounded seven-day chunks, then switches to five-minute incremental collection. It fetches current climate-zone snapshots and event-history ranges through GET-only HC3 endpoints.

## Run

The database must already exist and have the shared migrations applied. HC3 credentials are read from the existing FIBARO_HC3_* environment variables or FIBARO_HC3_CREDENTIALS_FILE; secrets are never printed.

```sh
python -m thermoflow_fibaro_collection /var/lib/thermoflow/thermoflow.sqlite3 --start 1795600000 --end 1795686400
```

Choose an explicit UTC Unix window no longer than seven days. The manual command is one-shot. The dedicated Compose profile runs the scheduler: it resumes a separate backfill cursor until the entire configured range is fetched, then starts incremental polling from the backfill end. An existing live cursor cannot cause earlier missing history to be skipped.

## Completeness semantics

- Event IDs are persisted idempotently; payload conflicts or invalid records make the run partial and do not advance the successful cursor.
- Backfill progress is tracked separately from the live cursor in the existing collector_state table, so an earlier partial import is rechecked from the configured initial timestamp.
- The backfill uses adaptive six-hour HC3 history windows, recursively splitting busy windows before the 800-record threshold and failing closed if a one-second window remains overloaded.
- After the complete backfill succeeds, the live cursor is initialized to the backfill end; subsequent cycles poll every five minutes with a 120-second overlap.
- Failed HC3 requests are recorded in collection_runs and collector_state with a sanitized error type; they never advance the successful cursor.
- The report includes the requested range, inserted/duplicate/invalid counts, stored event bounds, and snapshot identity.
- HC3 event history is event-driven. No events in a period is not proof of a collection outage, and the collector does not invent a sampling cadence or fill unobserved intervals.
- Snapshot capture is separate evidence from event-history coverage.

## Safety

All upstream calls are GET-only. No device commands, setpoint changes, background worker, forecast replacement, or heating-control integration is performed. Production database initialization and scheduling remain separate, explicit operational steps.
