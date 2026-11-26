# Explicit Fibaro HC3 collection

The thermoflow_fibaro_collection module provides a one-shot, bounded ingestion command for the shared ThermoFlow SQLite database. It fetches a current climate-zone snapshot and an event-history range through GET-only HC3 endpoints, then stores both through FibaroHistoryCollector.

## Run

The database must already exist and have the shared migrations applied. HC3 credentials are read from the existing FIBARO_HC3_* environment variables or FIBARO_HC3_CREDENTIALS_FILE; secrets are never printed.

```sh
python -m thermoflow_fibaro_collection /var/lib/thermoflow/thermoflow.sqlite3 --start 1795600000 --end 1795686400
```

Choose an explicit UTC Unix window no longer than seven days. The command is one-shot: it does not create a timer, worker, API startup hook, or recurring schedule. An operator or external scheduler must choose when to invoke it.

## Completeness semantics

- Event IDs are persisted idempotently; payload conflicts or invalid records make the run partial and do not advance the successful cursor.
- The report includes the requested range, inserted/duplicate/invalid counts, stored event bounds, and snapshot identity.
- HC3 event history is event-driven. No events in a period is not proof of a collection outage, and the collector does not invent a sampling cadence or fill unobserved intervals.
- Snapshot capture is separate evidence from event-history coverage.

## Safety

All upstream calls are GET-only. No device commands, setpoint changes, background worker, forecast replacement, or heating-control integration is performed. Production database initialization and scheduling remain separate, explicit operational steps.
