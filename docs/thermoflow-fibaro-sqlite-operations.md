# ThermoFlow Fibaro + SQLite operational contract

## Collection policy

- The dedicated Fibaro collector is opt-in through the Compose profile
  `fibaro-collector`; the normal API does not start a collector.
- The intended collection interval is five minutes.
- Each cycle reads HC3 event history incrementally from the last successful
  SQLite cursor, with a 120-second overlap for late-arriving events.
- A first collection requires an operator-supplied UTC Unix start timestamp.
- Long outages are replayed in bounded seven-day windows on subsequent cycles.
- HC3 event history is event-driven. Empty periods are not interpreted as
  collector outages, and missing days are never synthesized.
- A current climate-zone/head snapshot is checked each cycle. Only a changed
  snapshot content transition is stored; unchanged snapshots do not create
  duplicate history rows.

## Source and safety boundaries

- HC3 remains the source of truth for current zone and device state.
- The shared SQLite database is the durable local evidence/analytics store.
- The collector uses GET-only HC3 endpoints. It never changes zone membership,
  device configuration, setpoints, schedules, or heating settings.
- AI/ML analysis is offline shadow evaluation. It cannot write back to HC3,
  promote itself, or alter fuel/boiler control.
- If a future AI workflow proposes a zone/head reassignment, that must be a
  separate reviewed and explicitly authorized workflow with an audit trail.
  It must not be folded into telemetry collection.

## Operator sequence

1. Confirm the actual Portainer stack and persistent volume mapping.
2. Create a verified backup before any schema or maintenance operation.
3. Apply schema migrations explicitly; never rely on API startup to do this.
4. Verify schema version, WAL mode and SQLite integrity.
5. Configure HC3 credentials and the initial history timestamp.
6. Enable the optional `fibaro-collector` Compose profile.
7. Inspect collection run status and readiness output before using history for
   model evaluation.
8. Keep the collector disabled until the operator has approved the initial
   database setup and credentials.

## Readiness semantics

The read-only readiness report includes event counts and timestamps, distinct
observed UTC days, changed snapshot transitions, collection run outcomes and
snapshot freshness. It does not infer expected event frequency or mark a quiet
event-driven period as missing data. Existing model data-quality gates remain
authoritative for shadow evaluation.
