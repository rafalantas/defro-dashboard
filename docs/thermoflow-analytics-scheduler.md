# ThermoFlow local analytics scheduler

The analytics scheduler runs as a daemon thread inside the existing
`defro-dashboard-api` process. It does not create a third Compose service or
container. It uses the same SSD-backed SQLite database as the API and Fibaro
collector:

`/var/lib/thermoflow/thermoflow.sqlite3`

It performs three local stages:

1. Run a read-only Fibaro curation pass. Compare its deterministic content hash
   with the current `fibaro_heating_history_v2` candidate dataset.
2. If and only if the candidate content changed, use the existing curation
   apply path. That path creates and verifies a SQLite backup, inserts the
   candidate rows transactionally, and retains every raw Fibaro event.
3. Persist observed-only hourly/daily Fibaro/eModul aggregates, then run the
   existing offline Fibaro shadow pipeline. Model artifacts and predictions
   are written only when the existing quality, training and holdout gates pass.

## Runtime and data semantics

- The service reads the shared SQLite database only. It does not contact HC3,
  ThingSpeak or Open-Meteo.
- It does not create synthetic samples, interpolate quiet intervals, or infer
  historical head-to-zone membership.
- It does not change boiler, valve, pump, thermostat, schedule or Fibaro
  settings. Model promotion and production control are disabled.
- A changing curated source hash creates a verified backup in
  `/var/lib/thermoflow/backups` before candidate data is written. An unchanged
  hash skips the backup and curation write. The scheduler recognises a stored
  dataset through `dataset_version_for()` in `thermoflow_fibaro_curate.py`, so it
  always matches the name and version curation actually writes (an earlier copy
  of that rule looked for `v2` while curation stored `v3`, which made every cycle
  look changed and back up the whole database).
- Only the newest 2 curation backups (`thermoflow-before-fibaro-dataset-*.sqlite3`)
  are kept; older ones are removed after a successful write. Other files in that
  directory are never touched.
- Dataset versions are named by a hash of their full content, so every new batch
  of data would otherwise store a new full copy. After each cycle
  `thermoflow_dataset_retention` keeps only the newest candidate version per
  dataset (`fibaro_heating_history_v2/v3`, `thermoflow_heating_aggregates`) and
  removes older candidate versions together with their feature windows. It
  never touches raw evidence, promoted or otherwise non-candidate datasets,
  versions referenced by a model evaluation or model version, or any dataset
  outside that allow-list.
- One-off cleanup of versions already stored (dry run by default):
  `python -m thermoflow_dataset_retention /var/lib/thermoflow/thermoflow.sqlite3`
  then add `--apply`.
- SQLite write conflicts or a concurrent source change fail the current cycle
  safely; the next scheduled cycle retries.
- The default cadence is six hours, with a 120-second initial delay after API
  startup so the Fibaro collector can settle. Configure it with
  `THERMOFLOW_ANALYTICS_ENABLED`, `THERMOFLOW_ANALYTICS_INTERVAL_SECONDS` and
  `THERMOFLOW_ANALYTICS_INITIAL_DELAY_SECONDS`. The CLI remains available for
  operator-triggered one-off runs with `--once`.

## Model readiness

The shadow pipeline keeps its existing quality gates. In particular, its
training implementation requires at least 20 chronological training samples
and 5 untouched holdout samples. A 14-day event history is therefore expected
to remain in `collecting` until enough consecutive observed days accumulate.
That is a normal readiness state, not a scheduler failure.

## Current scope boundary

This service refreshes the Fibaro candidate dataset, analytical aggregates,
and the existing Fibaro offline shadow artifacts. It does not populate
`weather_forecasts` or `refill_events`; those need source-specific persistence
and reconciliation with the existing Open-Meteo and ThingSpeak providers.
The existing fuel estimates and dashboard behavior are not changed by this
scheduler.
