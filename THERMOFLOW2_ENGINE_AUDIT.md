### Regression fix: calibrated counter description

CI exposed a production-path regression after feeder calibration completed:
the method-description helper assumed every post-epoch coefficient carried
`uncertainty_percent`. Fully calibrated coefficients intentionally do not
carry an uncertainty estimate, so the summary path raised `KeyError`.
The helper now distinguishes fully calibrated, epoch-spanning estimate and
provisional coefficient states. Only the latter two display their declared
uncertainty. Dedicated unit tests cover all three states.

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
- **Planning extraction:** weather-aware kg/day, days-to-reserve and today/week/month/season pellet planning now live in `fuel_planning.py`; weather/network providers remain injected by the compatibility adapter.
- **Transport/error extraction:** upstream JSON transport and the shared `FuelError` contract now live in `fuel_http.py` and `fuel_errors.py`; `fuel.py` retains the legacy names as imports.\n- **Estimate contract:** `fuel_estimates.py` defines a provenance-preserving estimate record with explicit source, unit, measured/calibrated/provisional/predicted/unavailable status, uncertainty, coverage, calibration regime, model version and time window. It is deliberately not wired into production selection yet; the current precedence must be characterized first. Dedicated contract tests run in CI and the module is packaged in the API image.
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

### Current estimate-selection contract

The hopper's current-consumption selection is now represented by the pure
`fuel_estimates.select_usage_estimate()` function and covered by dedicated
precedence tests. The preserved order is:

1. valid counter-derived consumption (including its provisional/collecting label);
2. burner-energy estimate when calibration has at least 7 days;
3. refill/HDD house model;
4. burner-energy estimate when calibration is shorter than 7 days;
5. recent refill-period average;
6. unavailable when no source can produce a value.

The helper is now called by the legacy `fuel.py` path; it does not switch the
dashboard to the new typed `Estimate` object or change estimate units. This
also removes a latent fallback reference to an undefined local `periods`
variable: the fallback now uses the explicitly prepared recent refill periods.
Keep the selection tests as a migration gate before introducing any new
selection policy.

### Hopper counter provider extraction

The ThingSpeak hopper-percent counter read path is now in
`fuel_counter_provider.py`. It preserves the field-specific 8000-feed query,
same-value suppression, extension of the final known value to the last feed
timestamp, and historical-day cache behavior. The facade still owns the cache
and passes the existing channel, field, timezone and transport settings.
Dedicated tests cover transitions, repeated values, blank final feeds and
today's uncached data.

### Daily source arbitration extraction

`fuel_daily_provider.py` now owns the existing daily-source arbitration and
historical daily cache policy. ThingSpeak remains primary; Open-Meteo fills
missing temperature/HDD only, while valid burner-energy samples are retained.
A ThingSpeak transport failure still marks a warning and deliberately avoids
caching the fallback result so the primary source can recover on the next
request. Today's record remains uncached. The facade injects the existing
daily cache and source functions; tests cover primary, fallback, partial data,
failure recovery and cache behavior.

### ThingSpeak daily telemetry provider extraction

Daily ThingSpeak feed retrieval and channel-creation-date discovery now live in
`fuel_thingspeak_daily_provider.py`. The provider owns the UTC query-window
format, field parsing, sorted burner-power integration, maximum-gap cap and
daily temperature/HDD aggregation. The facade continues to own the historical
daily cache and the fallback decision between ThingSpeak and Open-Meteo.
Tests cover energy integration with capped gaps, empty feeds, configured start
dates and timezone conversion of channel creation timestamps.

### Open-Meteo and refill provider extraction

Open-Meteo temperature and weather retrieval now lives in
`fuel_weather_provider.py`; the facade injects location, timezone, endpoints,
transport, cache dictionaries, TTL and clocks. Historical archive requests stay
month-bounded, forecast windows and the 7-day archive/forecast boundary remain
unchanged, today's temperature is not frozen, future weather cache expires on
the existing forecast TTL, and API failures retain the last known optional
weather value.

ThingSpeak refill reads and writes now live in `fuel_refill_provider.py`.
The facade injects the existing 60-second refill cache, normalizer, transport
and cache invalidation callback. The 8000-feed read, UTC `created_at`, missing
channel 503 and rate-limit 429 contracts are covered by provider tests.
Neither provider changes refill normalization/30-minute merge semantics.

### Current extraction progress — telemetry provider

The ThingSpeak telemetry-history retrieval path has been moved behind
`fuel_telemetry_provider.fetch_history()`. The provider receives channel
configuration, timezone, cache storage/TTLs, bounded-window planner, transport
and analyzer as dependencies. The `fuel.py` facade still supplies the same
production globals and retains the existing cache object, 6-hour complete /
10-minute partial TTLs, 3-day request windows, 8-worker concurrency, 12-second
request timeout and partial-coverage payload. This is an infrastructure
extraction, not a change to telemetry features or estimate selection.

Dedicated tests cover complete fetch + cache reuse, partial upstream failure,
and the existing HTTP 503 contract for missing channels. The CI syntax/import
smoke test now includes the provider module.

### Refill-period analysis extraction

The refill-period calculation previously embedded in `fuel.py` now lives in
`fuel_period_analysis.py`. The module receives period aggregates, counter
observations, the current HDD model, calibration epoch and physical constants
through explicit dependencies. It does not own HTTP access, environment
configuration or caches.

The compatibility wrapper in `fuel.py` delegates to this module and preserves
the legacy period payload, including raw burner-energy estimate, missing-day
counts, HDD/energy ratios, optional heating breakdown, counter-floor behavior
and old/new/epoch classification. Dedicated tests cover the output arithmetic,
missing observations, invalid/overlong intervals, bounded recent history and
the feeder-epoch boundary. The API Docker image and ThermoFlow CI include the
new module and tests.

This is a responsibility extraction only; it does not change estimate
selection or make any shadow output production-visible.

### Stable engine interface

`fuel_engine.py` now defines the runtime contract consumed by `api.py`
(`handle()` and `warmup()`) and a `LegacyFuelEngine` adapter around the
current `fuel.py` implementation. API route dispatch now depends on this
interface rather than importing the legacy implementation directly. Loading
remains lazy: importing the core API or the interface does not import
`fuel.py`; a legacy import failure is still translated into the existing
fuel-subsystem-unavailable response. The adapter contract is covered by
dedicated tests, and the module is included in the API image and CI.

This is the stable replacement seam, not a cutover: the adapter still delegates
all production calculations to the existing legacy implementation.

## Recommended ThermoFlow 2.0 sequence

1. **Canonical contract:** shared schema/timezone, provenance-aware estimate contract and explicit legacy source-precedence function are in place and tested.
2. **API isolation:** make core API independent of fuel-engine import and warmup failures; preserve all route contracts.
3. **Provider and cache extraction:** continue with ThingSpeak/Open-Meteo daily/weather/refill providers and explicit cache ownership, retaining TTLs and failure semantics.
4. **Legacy adapter:** wrap current `fuel.py` behind a stable engine interface without changing its calculations.
5. **New engine implementation:** move domain logic into cohesive modules (refills, counter, energy, weather/HDD, telemetry features, forecasts/planning, estimates). Do not copy the monolith as one new file.
6. **Shadow parity:** feed identical normalized snapshots to legacy and new engines. Compare source-by-source outputs, calibration state, coverage and uncertainty; do not write shadow results into production dashboard fields.
7. **Operational validation:** test cold start, missing ThingSpeak channel, partial telemetry, stale cache, epoch-crossing refill periods, daylight-saving transitions and Docker images.
8. **Controlled cutover:** only after documented parity and explicit approval; keep a tested rollback to the legacy adapter.
9. **Cleanup:** remove legacy implementation only after stable operation has been demonstrated.

## Non-negotiable safety boundary

This is a fuel-estimation and dashboard-data migration only. Do not add commands or write paths for boiler, burner, feeder, 4D valve or pump control. Do not change controller configuration. The return-protection target remains **55 °C** and must not be modified by this project.
