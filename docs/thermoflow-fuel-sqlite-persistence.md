# ThermoFlow fuel event persistence

The fuel runtime now archives two existing source streams in the shared
ThermoFlow SQLite database. This is a fail-soft archival side path; current
fuel calculations, ThingSpeak writes, cache behavior and dashboard response
shapes remain unchanged.

## Refill events

When the existing ThingSpeak refill provider refreshes its feed, the raw feed
entries are archived in `refill_events` before the existing normalizer builds
the active refill list.

- Refill entries retain source entry ID, timestamp, amount and note.
- Void entries retain the source entry ID and the ID of the refill they cancel.
- Event keys use `thingspeak:fuel:<entry_id>`; already archived IDs are skipped.
- The first successful refresh imports the latest available ThingSpeak feed
  window; subsequent refreshes archive only newer source entry IDs.
- The active refill model continues to use the existing normalized ThingSpeak
  provider. The SQLite archive does not replace it or change void semantics.

## Weather forecasts

After the existing fuel summary has successfully built its Open-Meteo forecast,
the daily forecast rows are archived in `weather_forecasts`.

- `forecast_for` is local midnight in the configured fuel timezone (default
  `Europe/Warsaw`).
- Temperature, solar radiation and wind remain weather observations/forecasts.
- The optional pellet amount is explicitly labelled
  `fuel_model_prediction_not_measurement`; it is not a measured consumption.
- Identical consecutive daily payloads are skipped. A changed forecast creates
  a new fetched version for that target day.

## Storage and failure behavior

Both writers use the existing
`THERMOFLOW_DB_PATH=/var/lib/thermoflow/thermoflow.sqlite3` database. They
refuse to create a database file if the shared SSD database is missing.

Persistence errors are isolated from the fuel provider and summary paths. The
existing live estimate is returned even if archival storage is unavailable.
No HC3, boiler, pump, valve, thermostat or setpoint writes are introduced.
