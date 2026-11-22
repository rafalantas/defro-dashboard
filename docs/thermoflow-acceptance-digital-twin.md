# ThermoFlow PR #122 — acceptance evidence integration and Digital Twin foundation

## Scope

This change completes the connection between the production acceptance endpoint and the existing offline evidence modules, and adds a conservative, read-only thermal trend projection based on synchronized live eModul samples.

It does **not** deliver a calibrated physical heat-transfer simulator. The Digital Twin foundation estimates recent measured trends for boiler, return and flue temperatures over 5/15/30 minutes. Its uncertainty band is explicitly heuristic, not a probability interval. No actuator what-if simulation is claimed.

## Acceptance evidence wiring

`GET /api/thermoflow/v1/acceptance` now calls `build_acceptance_evidence` and passes its reports to the existing acceptance gate builder:

- Data Quality uses the existing `thermoflow_data_quality_gates` functions.
- Model Monitoring uses `thermoflow_model_monitoring` and the existing chronological shadow evaluator.
- Digital Twin freshness/state is a separate acceptance gate.
- Missing optional evidence remains `collecting` / `not_assessed`; malformed configured evidence becomes blocked/review.
- The endpoint reads only explicitly configured local files. It does not call ThingSpeak, Open-Meteo, eModul or any upstream service. It does not write files, train models or promote artifacts.

Optional evidence files are placed under the existing read-only `./data/thermoflow:/run/thermoflow:ro` mount. Configure paths in the API environment:

| Variable | Content |
|---|---|
| `THERMOFLOW_FIRE_SNAPSHOT_PATH` | Raw `thermoflow-raw-fire/1` snapshot |
| `THERMOFLOW_FUEL_SNAPSHOT_PATH` | Verified fuel periods with archived as-issued baselines and forecasts |
| `THERMOFLOW_VERIFIED_REFILL_IDS_PATH` | JSON object containing `verified_refill_ids` array |
| `THERMOFLOW_BURNER_MODEL_PATH` | Existing Burner live artifact |
| `THERMOFLOW_FUEL_MODEL_PATH` | Existing Fuel live artifact |
| `THERMOFLOW_BURNER_LEDGER_PATH` | Frozen `thermoflow-shadow-ledger/1` Burner evaluation ledger |
| `THERMOFLOW_FUEL_LEDGER_PATH` | Frozen `thermoflow-shadow-ledger/1` Fuel evaluation ledger |

The two model path defaults already point at `/run/thermoflow/burner-model.json` and `/run/thermoflow/fuel-model.json`. Snapshot and ledger paths default to empty. Missing files never pass a gate. Files are opened as regular files only, with an 8 MiB maximum and no symlink following.

The acceptance endpoint intentionally calls live ML read without a fuel summary. This keeps the endpoint free of remote I/O; Fuel ML may remain collecting here until a prediction is already available through its normal runtime path.

## Digital Twin foundation

The existing API poller is the only writer. The runtime keeps the existing bounded volatile RAM sample history; no new database, disk persistence, channel writes or polling loop is introduced.

A projection is emitted only when:
- the latest sample is fresh (at most 90 seconds);
- at least 8 valid samples exist;
- the contiguous window spans at least 5 minutes;
- no gap exceeds 75 seconds;
- at least two temperature signals have sufficient valid samples.

The model fits a simple linear trend over the observed contiguous window and projects the current measured trend to 5, 15 and 30 minutes. It reports sample coverage, trend slope, fit residual, current measured values and a heuristic range. Out-of-range projections are omitted. It does not infer missing sensor values or extrapolate across gaps.

The dashboard shows current values and trend projections in the Diagnostics → Production Acceptance card. When evidence is stale or insufficient it shows collecting/unavailable, not a synthetic forecast.

## Safety boundary

- Read-only and advisory only.
- No control writes, setpoint changes, pump/valve/feeder/ignition/STB actions.
- No claim of physical causality or calibrated probability.
- Return-temperature protection remains **55°C ±1°C** and is not changed.
- The feature does not replace operator judgement or production smoke tests.

## Validation

The PR adds focused tests for the digital twin, acceptance evidence loader, API integration and frontend rendering. The full ThermoFlow validation workflow remains the release gate.
