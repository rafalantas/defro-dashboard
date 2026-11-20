# ThermoFlow 2.0 — migration contract and baseline

**Baseline:** `main` at `1bdccf2eb1536642c5bf896b2398921341fdb4f1` (2026-11-19)  
**Purpose:** record the observable contracts that a replacement fuel engine must preserve. This document describes the repository baseline; it is not a proposal to change live boiler control.

## 1. Runtime topology

- `thermoflow`: nginx frontend, published as host port `5001` to container port `80`.
- `api`: Python 3.12 Alpine process, listens on `0.0.0.0:8000`; compose exposes it internally.
- `cloudflared`: optional compose profile `cloudflare`, depends on the frontend.
- The API process starts a daemon eModul poller (calls `snapshot()` every 30 seconds), calls `fuel.warmup()`, then starts `ThreadingHTTPServer`.
- `snapshot()` has a 20-second in-process cache. Statistics/history have separate 30-second caches.
- No database or local persistence is used by the fuel module for refill/daily aggregates; ThingSpeak is the durable store and most computed results are in-memory caches.
- `api.py` imports `fuel` at module import time. A future package split must preserve startup/import behavior or deliberately decouple the live status API from the fuel engine.
- `Dockerfile.api` explicitly copies runtime modules into `/app`; keep it synchronized when adding or moving files.

## 2. HTTP API contract

All successful JSON responses are UTF-8 and include `Cache-Control: no-store`. Unknown routes return HTTP 404. The legacy status/statistics/history handlers generally encode upstream failures in a JSON body while responding HTTP 200; do not silently change this behavior during a pure engine migration.

### Main API

| Method | Path | Query / behavior |
|---|---|---|
| GET | `/api/status` | Current eModul snapshot; cached; records compact ThingSpeak telemetry when connected |
| GET | `/api/health` | Subset of snapshot: `configured`, `connected`, `source`, `error`, `fetched_at` |
| GET | `/api/modules` | eModul modules, tiles, zones and menus |
| GET | `/api/diagnostics` | eModul diagnostics |
| GET | `/api/statistics` | `period` (day/month/year), `year`, `month`; reads ThingSpeak month/year history |
| GET | `/api/history` | `hours` 1–24 (default 6); optional `step` 60–3600 seconds; optional paired `start` and `end`; optional comma-separated `series`; `format=compact|full`; `analysis=0|false|no` disables analysis; optional `minimum_opening` 0–100 |

### Fuel API

| Method | Path | Query / body |
|---|---|---|
| GET | `/api/fuel/stats` | Optional `days` (e.g. 30) |
| GET | `/api/fuel/daily` | Optional `days` (e.g. 60) |
| GET | `/api/fuel/summary` | Summary for dashboard fuel card |
| GET | `/api/fuel/telemetry-audit` | Optional `days`, clamped to 1–14 |
| GET | `/api/fuel/refills` | Returns `{ "refills": [...] }` |
| POST | `/api/fuel/refill` | JSON `{ "kg": number }` or `{ "bags": number }`; optional `ts`, `note`; requires `X-Api-Token`; success 201 |
| DELETE | `/api/fuel/refill/<id>` | Voids a refill; requires `X-Api-Token` |

The refill assumption is material: each refill tops the hopper to the same level, so the amount added represents consumption since the previous refill; the first refill is a starting point, not a consumption interval. Refill records are voided rather than physically removed.

## 3. ThingSpeak data contract

### Compact telemetry channels

`THINGSPEAK_CHANNEL_IDS` and `THINGSPEAK_WRITE_KEYS` are comma-separated and positional. There must be exactly two channel IDs and two write keys for compact writes. `THINGSPEAK_READ_KEYS` is also positional. A snapshot is written no more often than `THINGSPEAK_WRITE_INTERVAL_SECONDS` (minimum 35 seconds, default 40) per channel. Boolean pump states are serialized as 0/1. Missing/non-numeric values are omitted.

Channel 1 — temperatures and boiler power:

| Field | Series ID | Meaning / unit |
|---|---|---|
| field1 | `thermoflow-co` | Boiler/CO temperature, °C |
| field2 | `thermoflow-dhw` | DHW temperature, °C |
| field3 | `thermoflow-flue` | Flue-gas temperature, °C |
| field4 | `thermoflow-outdoor` | Outdoor temperature, °C |
| field5 | `thermoflow-room` | Room temperature, °C |
| field6 | `thermoflow-floor` | Floor/buffer upper sensor C1, °C |
| field7 | `thermoflow-valve4d-current` | 4D valve current temperature, °C |
| field8 | `thermoflow-boiler-power` | Instantaneous boiler power, kW |

Channel 2 — valve, pumps and fuel level:

| Field | Series ID | Meaning / unit |
|---|---|---|
| field1 | `thermoflow-valve4d-effective-setpoint` | Corrected 4D valve setpoint, °C |
| field2 | `thermoflow-valve4d-opening` | 4D valve opening, % |
| field3 | `thermoflow-valve4d-return` | Return temperature, °C |
| field4 | `thermoflow-pump-valve4d` | 4D pump state, 0/1 |
| field5 | `thermoflow-pump-floor` | Underfloor pump state, 0/1 |
| field6 | `thermoflow-pump-dhw` | DHW pump state, 0/1 |
| field7 | `thermoflow-pump-circulation` | Circulation pump state, 0/1 |
| field8 | `thermoflow-fuel-percent` | Controller fuel-level/counter reading, % |

### Separate pellet refill channel

- `THINGSPEAK_FUEL_CHANNEL_ID` identifies the separate refill channel.
- field1 = kilograms added; field2 = note (defaults to `import` for the bulk importer); field3 = entry_id of a voided refill.
- `THINGSPEAK_FUEL_READ_KEY` and `THINGSPEAK_FUEL_WRITE_KEY` are separate credentials.
- Do not confuse field8 of compact channel 1 (boiler power) with field8 of compact channel 2 (fuel percent); the channel position is part of the schema.

### Current schema risk

The field mapping originally existed in multiple places. `fuel_schema.py` is now canonical; `api.py`, `fuel_telemetry.py`, and `thermoflow2_runtime.py` consume it and contract tests verify the mapping.

## 4. Configuration inventory

Secrets must remain in environment variables or secret files and must never be committed.

| Variable | Purpose / observed default |
|---|---|
| `TECH_API_TOKEN` / `TECH_API_TOKEN_FILE` | eModul bearer token; file takes precedence when configured |
| `TECH_MODULE_ID` | Optional eModul module selection |
| `THINGSPEAK_CHANNEL_IDS` | Positional compact telemetry channel IDs |
| `THINGSPEAK_WRITE_KEYS` / `THINGSPEAK_READ_KEYS` | Positional compact channel credentials |
| `THINGSPEAK_WRITE_INTERVAL_SECONDS` | Compact write interval; default 40 s, clamped to at least 35 s |
| `THINGSPEAK_WRITE_ENABLED` | Set false/0/no/off to disable compact writes; default enabled |
| `THINGSPEAK_FUEL_CHANNEL_ID` | Separate refill channel |
| `THINGSPEAK_FUEL_WRITE_KEY` / `THINGSPEAK_FUEL_READ_KEY` | Refill channel credentials |
| `FUEL_API_TOKEN` | Token for refill POST/DELETE |
| `FUEL_LAT` / `FUEL_LON` | Open-Meteo coordinates; module defaults to 52.74, 23.58 |
| `FUEL_COUNTER_EPOCH` | Counter calibration regime boundary; default in code is 2026-11-13 18:27 Europe/Warsaw |
| `FUEL_HDD_BASE_FIXED` | Fixed HDD base; blank/off/auto/none/false/0 means automatic; accepted numeric range 8–22 °C |
| `HDD_BASE` | Fallback HDD base, default 14 °C |
| `FUEL_TZ` | Fuel timezone, default Europe/Warsaw |
| `FUEL_TEMPS_DATA_START` | Optional start date for temperature data |
| `PELLET_BAG_KG` | Bag weight, default 15 kg |
| `PELLET_HOPPER_KG` | Hopper capacity; code default 228 kg |
| `PELLET_HOPPER_RESERVE_PERCENT` | Reserve warning threshold; code default 15% |
| `PELLET_KWH_PER_KG` | Pellet energy, default 4.8 kWh/kg |
| `BOILER_EFF` | Efficiency factor, default 0.88 |
| `CLOUDFLARE_TUNNEL_TOKEN` | Optional tunnel token under compose profile `cloudflare` |

Note: `.env.example` is not exhaustive relative to `docker-compose.yml`; compose also passes several pellet/HDD/telemetry settings. Keep the example and compose environment inventory synchronized as a separate config-cleanup task, not as an implicit side effect of engine replacement.

## 5. Data semantics and safety invariants

1. Preserve Europe/Warsaw local-day boundaries for refill timestamps, HDD and daily telemetry; preserve timezone-aware timestamps when moving data between modules.
2. Do not treat missing samples as zero, interpolate incomplete telemetry, or silently convert missing sensor values into measurements.
3. Keep raw room-temperature samples available for audit, but exclude invalid values (including 0 °C) from daily model calculations as current telemetry logic does.
4. Pump runtime is inferred only between consecutive samples no more than 300 seconds apart; unknown gaps are not counted as ON or OFF time. The 4D pump is audited but intentionally has no runtime aggregate.
5. Counter epochs represent a change in kg-per-percent calibration. Never apply one calibration coefficient across an epoch boundary without splitting/weighting the interval.
6. First refill is a baseline point; close refill events within 30 minutes are merged by current fuel logic; voided refill records remain part of the audit trail.
7. Keep measurement, counter calibration, energy-based estimation, daily model, weather inputs, forecast and AI shadow outputs distinguishable. Estimates must not be presented as measured refill consumption.
8. AI output is currently shadow-only; it must not change production estimates until separately validated and explicitly enabled.
9. The current fuel history uses a six-hour cache for telemetry feature history; the telemetry audit endpoint fetches at most 14 days. Do not accidentally multiply API traffic or remove cache boundaries during refactoring.
10. The dashboard's existing JSON keys, units, null/missing semantics, HTTP statuses, and error behavior are compatibility contracts until a versioned API change is agreed.

**Heating-control boundary:** this repository's fuel-engine migration must not alter boiler control, the 4D valve, pump control, or safety configuration. In particular, the return-protection target of 55 °C is outside this migration scope and must remain untouched.

## 6. Current test baseline and validation commands

Existing regression files include:
- `tests/test_fuel_ai.py`
- `tests/test_fuel_planning.py`
- `tests/test_fuel_telemetry.py`
- `tests/test_counter_epoch.py`
- `tests/test_hdd_base.py`
- `tests/test_daily_lag.py`
- `tests/test_history_analysis.py`
- `tests/test_overview.py`
- `tests/test_frontend_assets.py`

The validation workflow should run all of them. Locally, the fuel-focused checks are:
```sh
python -m py_compile api.py fuel.py thermoflow2_runtime.py fuel_engine.py fuel_ai.py fuel_telemetry.py
python -m unittest tests/test_fuel_ai.py tests/test_fuel_planning.py tests/test_fuel_telemetry.py
python tests/test_counter_epoch.py
python tests/test_hdd_base.py
python tests/test_daily_lag.py
python tests/test_history_analysis.py
```

## 7. Deferred item: bulk refill import idempotency

`tools/import_refills.py --send` posts all parsed rows to ThingSpeak bulk_update. It currently does not fetch existing entries or persist an import ledger. Blindly adding duplicate detection based only on date and kilograms could reject legitimate same-day refills or collide with manually entered events. A safe idempotency design needs a stable source-row identity (or explicit operator-supplied import ID) and a read-before-write reconciliation policy. Until that contract and tests exist, keep the importer behavior unchanged and require preview/review before `--send`.

## 8. Migration gates

- Gate A — baseline: this document and CI cover the current observable behavior.
- Gate B — canonical data contract: one shared ThingSpeak schema and typed/validated normalized records.
- Gate C — isolation: `/api/status` and health endpoints start and serve even if the fuel engine cannot import or initialize.
- Gate D — shadow comparison: old and new fuel engines consume identical input snapshots; only comparison output is persisted; dashboard remains on old engine.
- Gate E — cutover: preserve API response contracts, pass regression and Docker builds, observe stable shadow parity, and have a tested rollback path.
- Gate F — cleanup: remove legacy code only after production stability is demonstrated.

No boiler control or live runtime behavior is changed by this preparation package.
