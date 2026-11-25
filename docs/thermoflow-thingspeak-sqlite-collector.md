# ThingSpeak history collector — shared SQLite

## Scope

This is an explicit, pull-driven ingestion layer for selected compact ThingSpeak
telemetry channels and the pellet refill channel. ThingSpeak remains the
upstream source of truth. The collector stores original feed payloads and
provenance in the shared ThermoFlow SQLite schema; it does not replace the
existing fuel calculations, API read paths, caches, or refill commands.

## Safety boundary

- No network client or credentials are created by this module.
- The caller supplies a fetch function that must use ThingSpeak GET endpoints.
- No write endpoint, API key write credential, scheduler, background worker,
  API startup hook, or automatic database migration is added.
- No source feed is modified or deleted.
- The collector requires schema migrations to have already been applied and
  fails closed if the shared tables are absent.
- No automatic retention or pruning is performed in this step.

## Channel configuration

The caller explicitly supplies a mapping for each channel:

- `kind: "telemetry"` stores the original feed in `telemetry_samples`.
- `kind: "refills"` stores the original feed in `refill_events`.

Channel aliases become stable source names (`thingspeak:<alias>`); source
keys include the channel ID and ThingSpeak entry ID. This keeps IDs from
different channels distinct. The feed's remaining source fields are canonicalized and hashed. `entry_id` and `created_at` are stored once in the indexed identity/time columns rather than repeated inside the JSON payload. The collector compares both payload hash and timestamp when detecting replays, so a changed timestamp for an existing entry remains a conflict.

## Integrity and replay

- A repeated channel/entry ID with an identical payload is counted as a duplicate.
- A repeated ID with a different payload is a conflict; the original is never overwritten.
- Invalid timestamps, IDs, out-of-window records, and non-JSON payloads make
  the collection run partial.
- A partial run never advances channel cursors.
- Complete runs advance per-channel cursors monotonically.
- Feed windows are limited to seven days and each channel response to 8,000 feeds.
- Inserts, run metadata, and cursor updates commit in one SQLite transaction.

## Invocation contract

The module exposes `ThingSpeakHistoryCollector.collect_once(fetch_channel, channels, start, end)`.
The supplied callback receives a channel ID and UTC epoch window and returns
the parsed `feeds` list. The caller controls cadence and credentials. This PR
does not connect the collector to the production API or deploy it.

## Validation

Run:

```sh
python -m unittest discover -s tests -p 'test_thermoflow_thingspeak_collector.py' -v
```
