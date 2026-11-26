# ThermoFlow live ML — Burner + Fuel

This PR connects the existing offline benchmarks to a live, read-only inference
runtime and the Unified Intelligence dashboard. Model training and artifact
creation remain offline. The API loads only small, versioned artifacts mounted
read-only into the API container.

## 1. Build the Burner ML artifact

```bash
python thermoflow_fire_snapshot.py --days 14 > fire-snapshot.json
python thermoflow_observed_cycles.py fire-snapshot.json > observed-cycles.json
python thermoflow_burner_ml.py observed-cycles.json > burner-benchmark.json
python thermoflow_burner_live.py burner-benchmark.json > burner-model.json
```

The exporter refuses to create an artifact unless the candidate beats both the
training-median and previous-cycle baselines on the untouched chronological
holdout. At the beginning of a new FIRE-on cycle, the live runtime uses only
the immediately preceding complete FIRE-confirmed cycle duration and the
observed idle time. It displays the predicted total cycle duration and elapsed
time; it does not claim to predict ignition in advance or remaining time.

A cycle is accepted only when its OFF → FIRE boundary was observed by the
running poller and consecutive observations are no more than 75 seconds apart.
If the process starts while FIRE is already on, or a telemetry gap occurs while
FIRE remains on, that partial cycle is not used as a completed training example.
The next complete OFF → FIRE → OFF cycle is required. A live prediction is also
withdrawn when the active cycle exceeds the runtime's six-hour evidence bound,
or when telemetry expires.

## 2. Build the Fuel ML artifact

Fuel ML requires **verified refill IDs and archived as-issued baselines**. An
assumed fill level, a controller percentage reset, or a reconstructed replay
baseline cannot qualify as a verified training label.

Prepare a metadata file using the existing
`thermoflow-fuel-import-metadata/1` contract. It must contain the physically
reviewed `verified_refill_ids` and an `archived_baselines` entry for each
period, with its original `start`, `baseline_kg_per_day`, and
`baseline_issued_at` (issued no later than the period start).

```bash
python thermoflow_fuel_snapshot.py --days 365 --metadata verified-refills.json > fuel-snapshot.json
python thermoflow_fuel_ml.py fuel-snapshot.json > fuel-benchmark.json
python thermoflow_fuel_live.py fuel-snapshot.json fuel-benchmark.json > fuel-model.json
```

The exporter requires the candidate to beat both the as-issued baseline and
previous-period baseline on the five-sample chronological holdout. The most
recent verified period must be no older than 14 days. At runtime, Fuel ML uses
the existing fuel planner's fresh `planning.today.average_kg_day` as the
current baseline feature and the latest verified measured period rate from the
artifact as the second feature. It returns an advisory kg/day forecast only;
it does not replace the existing hopper estimate or fuel accounting.

## 3. Mount artifacts into the API container

Place the generated files in the host-side `./data/thermoflow/` directory:

- `./data/thermoflow/burner-model.json`
- `./data/thermoflow/fuel-model.json`

The supplied `docker-compose.yml` mounts that directory at `/run/thermoflow`
read-only and sets both model paths by default. Either artifact may be omitted;
the corresponding module remains in a collecting/missing-artifact state. The
paths can also be overridden with `THERMOFLOW_BURNER_MODEL_PATH` and
`THERMOFLOW_FUEL_MODEL_PATH`.

Either model may be configured independently. Missing, invalid, oversized,
future-dated, or stale artifacts fail closed. Burner artifacts are limited to
30 days; Fuel artifacts are limited to 30 days and additionally require a
verified refill period no older than 14 days. The API reloads artifacts every
60 seconds. No training, network fetch, artifact write, database migration, or
new writable volume is introduced.

## 4. API and dashboard

`GET /api/thermoflow/v1/intelligence` now includes `live_predictions` with
separate `burner_ml` and `fuel_ml` states. The Diagnostics → Unified
Intelligence cards show the current prediction when a valid model and fresh
inputs exist; otherwise they explain whether data is still collecting, stale,
or unavailable. Existing offline holdout metrics remain visible separately.

## Safety boundary

- All predictions are advisory and read-only.
- No eModul or ThingSpeak write is performed by ML inference.
- No boiler setpoint, pump, valve, feeder, ignition, or STB action is exposed.
- No prediction is used to alter the existing fuel summary, hopper inventory,
  or its planning forecast.
- Return-temperature protection remains **55°C ±1°C**, unchanged.
- Without physically verified refill provenance, Fuel ML stays unavailable
  rather than silently learning from assumed refill quantities.


## Live outcome persistence (SQLite shadow)

When the live runtime produces a Burner prediction from a checksum-verified,
SQLite-registered model, it inserts one pending row into shadow_predictions.
The row is keyed by model version and the confirmed FIRE-on transition
timestamp. A matching FIRE-off transition in the same continuous observation
session updates that exact row with the observed cycle duration and marks it
evaluated.

A process restart, disconnected sample, or telemetry gap longer than the
runtime continuity limit breaks cycle ownership. Any already-persisted row
then remains pending; a later FIRE-off event is never attached to it. The
file-artifact compatibility fallback does not write live outcomes. Persistence
is observational and failures do not affect telemetry polling, predictions,
or controller behavior. No migration, collector, training, or model promotion
is started by the API runtime.


## Fuel ML — verified SQLite artifacts

The optional explicit Fuel ML importer stores the content-addressed verified
refill dataset, checksum-protected inference artifact, holdout metrics and
paired holdout predictions in the existing shared SQLite schema. It accepts
only `as_issued` baselines and `verified_equal_fill_level` labels. Every
training and holdout sample is checked against its source refill interval,
adjacent verified period and calibration regime before persistence.

The importer does not fetch ThingSpeak data, infer refill mass, train from the
scheduler, change the existing fuel forecast, or promote a model. Model
promotion remains a manual review decision.
