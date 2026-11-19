# ThermoFlow 2.0 — fuel engine readiness audit

**Audited revision:** `main` at `3392c3e39acab3436c6182c00cf6502778cf476e`  
**Scope:** `fuel.py`, `fuel_ai.py`, `fuel_telemetry.py`, API integration in `api.py`, current tests and `Dockerfile.api`.  
**Purpose:** identify migration blockers and behavioral contracts before replacing the legacy fuel engine. This is an audit only; no production runtime code is changed.

## Executive assessment

The current implementation is not one interchangeable estimator. It is a collection of models and evidence sources with different trust levels:

1. refill-to-refill consumption and HDD regression;
2. feeder percentage counter calibration, split around the feeder-change epoch;
3. burner-power energy estimate, calibrated against refill history;
4. daily counter-derived training rows and an optional daily weather model;
5. weather forecast and hopper/reserve planning;
6. experimental robust-regression AI, explicitly shadow-only;
7. read-only telemetry quality audit and daily aggregates.

ThermoFlow 2.0 must preserve these as distinguishable sources. It must never silently relabel an estimate as a measurement, or promote an experimental model merely because it returns a number.

**No production changes are recommended as part of this audit.** The dashboard must not gain boiler-control behavior. The 4D valve, pumps, controller settings and especially the 55 °C return-protection target remain outside the migration scope.

## Implementation progress on this branch

The first refactor stage has now addressed several audit findings without replacing production calculations:

- **F1 — API isolation:** `api.py` no longer imports `fuel` at module load. Fuel routes load the optional engine lazily; warmup runs in a daemon thread after the HTTP server is created. A fuel import/warmup failure no longer prevents the core server from starting.
- **F3 — calibration windows:** summary calibration now uses the same full refill history as statistics; the recent seven-refill window remains separate for recent-period display metrics. A focused regression test protects this distinction.
- **F4 — AI validation:** AI output now reports both expanding-window walk-forward scores and a fixed holdout score whose scored rows are never used for fitting. Existing top-level score fields and shadow-only behavior remain for compatibility; the acceptance flag still follows the existing walk-forward gain threshold.
- **F6 — ThingSpeak schema:** `fuel_schema.py` is now the canonical definition of both eight-field channels. API writes/history, fuel field indexes and telemetry parsing derive from it. Contract tests cover field order and semantic IDs.
- **F7 — timezone:** API, fuel and telemetry now obtain their timezone from the shared schema configuration, retaining Europe/Warsaw as the default.
- **F8 — telemetry fault isolation:** daily feature construction now converts unexpected telemetry exceptions into an explicit `unavailable` state; counter/refill estimates continue and summary/daily payloads expose the telemetry status.
- **F5 — telemetry fetch budget:** internal history now uses non-overlapping three-day windows instead of one request per day (82 requests rather than 242 at 120 days for two channels). The three-day window stays below ThingSpeak's 8,000-feed limit at the configured minimum 35-second write interval, including a 73-hour DST window. Failed windows are retained as partial coverage, successful windows remain usable, and partial results have a 10-minute cache rather than the normal six hours.
- **Pure model extraction:** the solver, OLS/LOO scoring, lagged-temperature transform, HDD regression and prediction functions now live in `fuel_models.py`.
- **Refill domain extraction:** normalization of ThingSpeak refill/void events and the 30-minute close-event merge now live in `fuel_refills.py`.
- **Counter domain extraction:** old/new feeder calibration, epoch-spanning estimates, provisional coefficients and interval splitting now live in `fuel_counter.py`.
- **Burner-energy extraction:** energy calibration and kWh-to-kg conversion, including epoch splitting, now live in `fuel_energy.py`.
- `fuel.py` re-exports these operations through compatibility wrappers, so existing callers and tests keep their names and payload behavior.
- Docker packaging and CI include all extracted modules, plus focused regression tests.

These are branch changes only. They do not constitute a production cutover; `fuel.py` remains the production calculation path, and AI remains shadow-only. CI must pass on the latest branch head before this stage is considered validated.

## Module inventory

### `fuel.py` — legacy production engine

Approximately 1,779 lines. Responsibilities currently include:

- ThingSpeak and Open-Meteo HTTP access, local-day boundaries and caches;
- refill read/write/void operations and close-event merging;
- refill-period regression and HDD base fitting;
- feeder counter calibration, epoch splitting and daily consumption;
- burner-energy conversion and calibration;
- daily weather model, forecasts, hopper reserve and planning horizons;
- summary, statistics and daily API payloads;
- telemetry-history fetching and telemetry audit orchestration;
- background warmup and HTTP route dispatch.

This module is both domain logic and infrastructure/API adapter. It should be decomposed by responsibility, not mechanically renamed.

### `fuel_ai.py` — experimental shadow model

Approximately 11.9 KB. It has no network access, file writes or imports from the production engine. It:

- filters incomplete, provisional and epoch-transition training rows;
- fits robust regression using Huber-style reweighting and ridge regularization;
- compares temperature-only and weather-aware models;
- optionally compares a telemetry-extended candidate;
- reports MAE, RMSE, bias, feature coefficients and holdout date ranges;
- labels its output `shadow_only` and states that production estimates are unchanged.

It is currently a model evaluator, **not a drop-in replacement for `fuel.py`**. It does not own refill CRUD, counter calibration, burner-energy estimation, weather retrieval, hopper state, planning, API response compatibility or cache orchestration.

### `fuel_telemetry.py` — read-only telemetry analysis

Approximately 8.3 KB. It normalizes two compact ThingSpeak channels, audits 16 series, reports sample coverage/gaps, creates daily aggregates and infers pump runtime only across consecutive samples no more than 300 seconds apart. The 4D pump is intentionally audited without a runtime total. Invalid room-temperature samples remain visible in raw series counts but are excluded from daily room-temperature aggregates.

Keep this read-only analysis capability separate from the fuel estimator.

## Findings and migration blockers

### F1 — API startup is coupled to the fuel engine

**Evidence:** `api.py` imports `fuel` at module import time; `fuel.py` imports both `fuel_ai` and `fuel_telemetry`. Startup also calls `fuel.warmup()` before creating the HTTP server.

**Risk:** an import-time failure in any fuel dependency can prevent the entire API from starting, including unrelated status and health endpoints.

**Required direction:** make fuel-engine loading/initialization independent of the core eModul API. The status/health service must start and remain usable when the fuel engine is unavailable. Preserve existing fuel routes and response/error contracts during the transition.

**Priority:** critical migration gate.

### F2 — multiple fuel estimates have different semantics and precedence

**Evidence:** `fuel.py` computes refill/HDD regression, counter-derived kg, burner-energy kg, daily weather-model estimates and forecast estimates. The summary chooses between counter, burner energy, house model and recent-period fallback depending on data availability and calibration state.

**Risk:** a rewrite that returns one generic `kg` field can erase the distinction between measured refill quantities, counter-derived consumption, energy-derived estimates and model predictions. A changed fallback order can create an apparent consumption change without any physical change.

**Required direction:** define typed estimate records with at least: value, unit, source, measured/estimated status, calibration regime, uncertainty, coverage/completeness, timestamp/window and model version. Document and test the current source-selection precedence before changing it.

**Priority:** critical migration gate.

### F3 — calibration windows differ between endpoints

**Evidence:** `_stats()` fits the refill model and calculates burner calibration from the full refill history. `_summary_build()` fits the house model from all refills but passes only `refills[-7:]` into `_periods()` and `_counter_calibration()`. `_daily_rows()` calculates counter calibration from the full refill history.

**Risk:** the same physical period can be represented using different calibration evidence depending on which endpoint is queried. In particular, summary counter calibration can lose older pre-epoch evidence once it falls outside the last seven refill records, while daily rows still use it.

**Required direction:** define named calibration windows and one shared calibration result per regime. If a recent-only calibration is intentional, expose it explicitly as a separate calibration rather than silently substituting it for the all-history calibration. Add tests where more than seven refills span the counter epoch.

**Priority:** critical correctness issue.

### F4 — AI validation is walk-forward, not a frozen final holdout

**Evidence:** `fuel_ai.evaluate()` initially splits chronological training and test rows, but inside the test loop fits each next prediction using `clean[:train_count + index]`. Earlier test observations therefore become training observations for later test predictions. The module docstring says the final chronological holdout is never used to fit scored models.

**Risk:** this is a legitimate expanding-window / walk-forward evaluation design, but it is not a fixed untouched holdout as the docstring implies. Its metrics answer a different question: performance when the model is refit as new actual daily values arrive.

**Required direction:** choose and name the validation protocol explicitly. Prefer reporting both (a) a fixed untouched holdout for model selection and (b) walk-forward performance for operational monitoring, if sufficient history exists. Keep AI shadow-only until the chosen protocol and acceptance thresholds are documented and pass on real, representative data.

**Priority:** high; do not promote the AI model on current wording alone.

### F5 — telemetry history fetch volume and comments disagree

**Evidence:** `_daily_rows(days)` calls `_telemetry_history(min(MAX_DAYS, max(1, days)))`. With `MAX_DAYS = 120`, the history helper schedules `2 * (days + 1)` daily channel requests: up to 242 requests, using eight workers and a 12-second request timeout. Its cache is six hours. A nearby comment says the audit is limited to 14 days, but that is true only of the public `_telemetry_audit()` endpoint, not this internal history path.

**Risk:** cold-start and cache-expiry load can be substantial; a 14-day assumption would understate the request count. A single failed request currently aborts the complete history fetch.

**Required direction:** make history depth, per-channel coverage and fetch budget explicit configuration/contracts. Consider fetching bounded ranges and recording partial coverage rather than losing the entire telemetry feature set on one failed day. Measure actual ThingSpeak latency and rate-limit behavior before changing concurrency. Do not reduce history to 14 days without accounting for the AI telemetry candidate's 45 training + 7 test day requirement.

**Priority:** high operational risk.

### F6 — ThingSpeak schema is duplicated across modules

**Evidence:** compact field definitions live in `api.py`; `fuel_telemetry.py` maintains its own `CHANNELS`; `fuel.py` separately uses positional field constants and channel indexes. The migration contract already records this risk.

**Risk:** field order or identity can drift. In particular, channel position matters: field 8 is boiler power on channel 1 and fuel percent on channel 2.

**Required direction:** define one canonical schema module with channel identity, field number, stable series ID, semantic key, unit and value kind. Have API writes, fuel reads and telemetry parsing consume it. Add a contract test that asserts all 16 compact fields and channel ordering.

**Priority:** high; do before moving modules.

### F7 — timezone configuration is not consistently shared

**Evidence:** `fuel.py` uses `FUEL_TZ` (default `Europe/Warsaw`); `fuel_telemetry.py` hardcodes `Europe/Warsaw`, and the telemetry request path also sends a literal Warsaw timezone.

**Risk:** current default behavior agrees, but changing `FUEL_TZ` would split day aggregation and fuel calculations across different local-day boundaries.

**Required direction:** inject a shared timezone/config object into normalization and aggregation. Preserve Warsaw day boundaries as the current production default and add a daylight-saving transition test.

**Priority:** medium; required for clean modularization.

### F8 — telemetry analysis exceptions are not fully isolated

**Evidence:** `_daily_rows()` catches `FuelError` around `_telemetry_history()`, but unexpected exceptions from `fuel_telemetry.analyze()` are not converted into a telemetry-unavailable result there. The summary's outer background job catches unexpected errors only after the summary build has failed.

**Risk:** malformed/unexpected telemetry can make the complete summary unavailable even though telemetry is an optional model feature.

**Required direction:** isolate telemetry normalization/feature construction behind a typed result with explicit `ready`, `partial` and `unavailable` states. A telemetry failure must not suppress refill, counter or burner-based production estimates.

**Priority:** high resilience issue.

### F9 — daily model and AI model are separate candidates with different validation

**Evidence:** `_fit_daily_model()` evaluates OLS candidates using leave-one-out error on the same usable history, with sign checks and a 3% improvement threshold. `fuel_ai.evaluate()` uses robust regression and chronological expanding-window scoring with separate 45/7-day minimums.

**Risk:** the dashboard can expose two different model outputs and acceptance semantics. A simple “AI accepted” flag is not enough to establish that it should replace the daily model.

**Required direction:** keep model identity, training set, validation protocol, score window, feature set and acceptance threshold explicit. Do not merge the two models into one opaque score.

**Priority:** high.

### F10 — exception and cache contracts are part of runtime behavior

**Evidence:** summary generation is backgrounded, uses a 60-second summary cache, waits up to 25 seconds on a cold read, and returns a `warming` response if work is still running. Telemetry history has a six-hour cache; other data has independent caches.

**Risk:** changing these while extracting the engine can alter endpoint latency, stale-data behavior and upstream request volume even if calculated values remain identical.

**Required direction:** preserve cache TTLs, cold-start behavior and failure semantics initially. Change them only with separate tests and operational evidence.

**Priority:** medium/high.

## Existing capabilities that must survive replacement

- Refill history, note, timestamp, POST creation, DELETE voiding and audit trail.
- Close refill event merge within 30 minutes; first refill remains a baseline, not a consumption interval.
- Counter calibration split at `FUEL_COUNTER_EPOCH`; never apply one coefficient across the feeder-change boundary.
- Distinction between calibrated and provisional counter coefficients, including uncertainty.
- Burner-energy estimate and calibration against refill periods.
- Refill/HDD building model, configurable/fitted HDD base and Open-Meteo offset correction.
- Daily counter rows, lagged temperature, weather features and daily model.
- Seven-day forecast, five-year monthly climate fallback, hopper capacity/reserve and days-to-reserve planning.
- Telemetry audit of all 16 series, sample gaps, invalid room samples and pump known/runtime semantics.
- Existing `/api/fuel/*` paths, JSON keys, units, null behavior, HTTP statuses and authentication behavior.
- Background summary warmup and cache behavior until a separately validated replacement is ready.

## Recommended ThermoFlow 2.0 sequence

1. **Canonical contract:** introduce shared schema/config/data types and tests; no calculation changes.
2. **API isolation:** make core API independent of fuel-engine import and warmup failures; preserve all route contracts.
3. **Legacy adapter:** wrap current `fuel.py` behind a stable engine interface without changing its calculations.
4. **New engine implementation:** move domain logic into cohesive modules (refills, counter, energy, weather/HDD, telemetry features, forecasts/planning, estimates). Do not copy the 1,779-line module as one new file.
5. **Shadow parity:** feed identical normalized snapshots to legacy and new engines. Compare source-by-source outputs, calibration state, coverage and uncertainty; do not write shadow results into production dashboard fields.
6. **Operational validation:** test cold start, missing ThingSpeak channel, partial telemetry, stale cache, epoch-crossing refill periods, daylight-saving transitions and Docker images.
7. **Controlled cutover:** only after documented parity and explicit approval; keep a tested rollback to the legacy adapter.
8. **Cleanup:** remove legacy implementation only after stable operation has been demonstrated.

## Non-negotiable safety boundary

This is a fuel-estimation and dashboard-data migration only. Do not add commands or write paths for boiler, burner, feeder, 4D valve or pump control. Do not change controller configuration. The return-protection target remains **55 °C** and must not be modified by this project.
