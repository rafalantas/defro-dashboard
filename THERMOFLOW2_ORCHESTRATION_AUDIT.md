# ThermoFlow 2.0 — remaining orchestration audit

**Branch:** `audit/thermoflow2-engine-readiness`  
**Baseline:** `a897a9bc5f281bd6e2d4b3cf9ee0ae7df91689c0`  
**Purpose:** map the remaining production orchestration before moving it out of `fuel.py`. This document makes no runtime changes.

## Decision

Do not replace `fuel.py` in one pass and do not create a second monolith named `fuel_engine_v2.py`. The extracted modules now cover most deterministic domain calculations and external providers. The remaining hard part is the orchestration that combines them, owns shared cache state, and produces the public API payloads.

The next implementation should introduce a dependency container/service boundary, then move one endpoint workflow at a time behind it. The compatibility facade must continue to expose `handle()`, `warmup()` and the existing private wrappers until parity is demonstrated.

## Remaining responsibilities in `fuel.py`

| Responsibility | Current functions / state | Dependencies and contract |
|---|---|---|
| Runtime configuration | module constants, `_csv_env`, `_parse_hdd_base_fixed`, `_parse_epoch` | Environment variables, canonical schema/timezone, physical constants, feeder epoch. Configuration must be immutable for one engine instance. |
| Shared cache state | `_daily_cache`, `_om_cache`, `_weather_cache`, `_weather_fetched`, `_refills_cache`, `_model_cache`, `_counter_cache`, `_daily_model_cache`, `_telemetry_history_cache`, offset/forecast/climate/summary caches | Existing TTLs, cache keys, invalidation, partial-result rules and process lifetime are observable behavior. |
| Local time and source composition | `_day_bounds`, `_get_daily`, `_period_temps`, `_period_sums`, `_om_offset`, `_forecast`, `_climate_monthly` | Warsaw local-day boundaries, DST, ThingSpeak/Open-Meteo arbitration, HDD-base correction, weather fallback. |
| Counter daily feature construction | `_counter_drop`, `_counter_day_kg`, `_daily_rows` | Counter epoch, previous-day boundary points, complete-day test, 20-hour pump coverage and 1000-sample analog threshold. Telemetry remains optional. |
| Statistics workflow | `_stats` | Parallel prefetch, lock boundary, refill/model/period calibration order, burner estimate enrichment, totals and exact response keys. |
| Summary workflow | `_summary_build`, `_summary_calibration_inputs`, `_summary_job`, `_summary_kick`, `_summary`, `warmup` | Background worker lifecycle, stale-while-refresh behavior, 60-second cache, 25-second cold wait, calibration windows, forecast/planning, AI shadow isolation. |
| Daily workflow | `_daily` | Prefetch order, lock boundary, house model and daily model fitting, residuals and telemetry state. |
| Refill commands and auth | `_add_refill`, `_void_refill`, `_check_token`, `_invalidate_caches`, `_ts_write` | Input validation, ISO timestamps, bag-to-kg conversion, API token comparison, void audit record, exact HTTP statuses and cache invalidation. |
| Public API routing | `handle` | Exact route/method matching, query defaults and bounds, payload compatibility, `FuelError` mapping. |
| Telemetry audit | `_telemetry_fetch_windows`, `_telemetry_history`, `_telemetry_audit` | Separate cached feature history vs uncached public audit, two-channel requirements, 14-day public bound, per-day 8000-feed request limit. |

## Endpoint dependency graph

### `GET /api/fuel/stats`

1. Validate and clamp `days` (default 30, max 120).
2. Prefetch requested daily source records concurrently.
3. Under the shared lock: load refills, fit house model, build refill periods, then read daily rows.
4. Outside the lock: calculate burner calibration, enrich daily rows with burner-derived kg and HDD ratios, and aggregate totals.
5. Return model, offset, config, both calibration states, summary, refill count, periods, daily rows and errors.

**Parity-sensitive details:** model fitting precedes period construction because the fitted HDD base affects HDD values; a failed refill/model stage produces empty refill/period collections and an error entry while daily data is still attempted.

### `GET /api/fuel/daily`

1. Validate and clamp `days` (default 60, max 120).
2. Prefetch `days + 2`.
3. Under the shared lock: load refills, fit house model, build counter/telemetry daily rows and fit the daily weather model.
4. Enrich rows with house-model prediction and counter residual where available.
5. Return both models, counter calibration, telemetry state, weather units and daily rows.

**Parity-sensitive details:** the daily model is trained from the same rows returned by the provider; do not include incomplete, epoch-transition or provisional rows differently during migration.

### `GET /api/fuel/summary`

1. Return `configured:false` when the refill channel is absent.
2. Return cached summary immediately; kick background refresh when stale.
3. On cold start, start one background job and wait at most 25 seconds.
4. Background job prefetches, loads all refills, fits the house model, builds all-history calibration plus separate recent display periods, and fetches the last seven complete days.
5. It then builds daily rows/model, evaluates AI in shadow-only mode, calculates weekly and hopper estimates, forecast and planning, and finally stores the complete response.
6. If the cold worker finishes with a recorded error, return HTTP 502 through `FuelError`; otherwise preserve the `warming:true` response.

**Parity-sensitive details:** never let AI evaluation failure invalidate deterministic estimates; do not replace the all-history calibration with the recent seven-refill display window; keep the existing counter/burner/model/recent-period precedence.

### Refill command validation and orchestration now lives in `fuel_refill_service.py`; `fuel.py` retains the HTTP body decoding and token check, and injects the existing ThingSpeak writer/raw-refill reader into the service. The command service preserves kg-versus-bags conversion, the 0–1000 kg range, ISO timestamp parsing and Warsaw timezone for naive timestamps, 255-character note truncation, single-raw-entry void semantics, and existing 400/404 errors. The existing ThingSpeak writer remains responsible for UTC `created_at`, 503/429 handling and the exact three-cache invalidation set.

Refill writes

- POST accepts either `kg` or `bags`; bag conversion uses configured bag mass.
- `ts` accepts ISO 8601 and naive values are interpreted in the configured local timezone.
- `note` is truncated to 255 characters.
- DELETE voids one raw source entry by writing its ID to field 3; merged refill events are recomputed from raw records.
- A successful write invalidates refill, house-model and summary caches; it does not invalidate immutable historical daily/counter/weather data.

## Concurrency and cache risks

1. The module-level `_lock` currently protects selected multi-step workflows, not every provider read. Do not assume it is a universal cache lock.
2. Summary background state has its own `_summary_guard`; it prevents duplicate workers and is separate from the calculation lock.
3. The summary cache stores a complete assembled payload. Moving calculations to a service must not accidentally cache partial/warming payloads as completed results.
4. The house model cache key includes refill count, latest refill ID, Open-Meteo offset and fixed HDD base. Daily model cache has its own key and dependency set.
5. Successful refill writes must preserve the exact invalidation set. Broad cache flushing would increase upstream requests and change cold-start behavior.
6. Partial telemetry history has a shorter cache TTL than complete history. The public telemetry audit is a separate request path and should not be silently routed through the cached daily-feature provider.

## Recommended extraction order

1. **Create an immutable runtime configuration object** from the existing module constants and environment parsing. Test defaults, invalid values, epoch timezone normalization and fixed HDD-base parsing. Do not change defaults.
2. **Create an explicit cache-state container** that holds the existing dictionaries and locks. Initially pass the same cache objects to the existing providers; do not change TTLs or key formats.
3. **Extract endpoint workflow services**: stats (`fuel_stats_service.py`), daily (`fuel_daily_service.py`) and summary calculation (`fuel_summary_service.py`) now receive providers/models/calculators as explicit dependencies and return the existing payloads. Keep HTTP parsing, status mapping, summary cache and worker lifecycle in the compatibility facade until their own parity tests are in place.
4. **Extract refill command service** with the existing validation/auth/void semantics and explicit invalidation callback.
5. **Implement the new engine adapter** using these services. Initially run it in shadow mode against the legacy engine on identical normalized inputs.
6. **Build a sanitized historical fixture** from real ThingSpeak history. Remove API keys, channel IDs, account metadata and any unrelated telemetry. Preserve timestamps, nulls, gaps, refill IDs/amounts, feeder epoch and source coverage because they affect the calculations.
7. **Compare source-by-source outputs**: refill periods, house HDD base/model, counter calibration and epoch regime, burner calibration, daily model, source selection, weekly estimate, last-refill estimate, hopper reserve, forecast and planning. Define tolerances per numeric field and exact comparison for statuses, provenance, units, nulls and keys.
8. Only after the fixture and live shadow run show stable parity should the new engine become selectable. Keep the legacy engine as rollback until an explicitly approved cutover.

## Safety and release gates

- This is read-only fuel estimation/dashboard work. Do not add controller writes or control logic.
- Preserve the return-protection target at **55 °C**; do not alter boiler, burner, feeder, valve or pump settings.
- Do not merge the PR, deploy, or switch production engine without explicit approval.
- A passing synthetic unit suite is necessary but not sufficient. The inactive legacy `fuel.py` requires a representative historical-data replay and then a live shadow comparison before cutover.


## First runtime-state extraction

The existing cache dictionaries and two synchronization locks have been grouped
in `fuel_runtime.FuelCacheState`. The legacy facade now creates one state
instance and aliases its original module-level names to the same objects.
This is intentionally a storage-only extraction:

- cache dictionary keys and initial shapes are unchanged;
- all TTL constants remain in `fuel.py`;
- provider cache policies remain unchanged;
- successful refill invalidation still clears exactly the same three caches;
- no cache persistence, eviction, or cross-process sharing was introduced;
- each engine-state instance owns independent dictionaries and locks.

The module is packaged in the API image and covered by CI tests for legacy
initial shapes, instance isolation and lock isolation. This creates a seam for
the future engine object without changing current module-level callers.


## Endpoint service extraction progress

The stats endpoint orchestration now lives in `fuel_stats_service.py`; the
daily endpoint orchestration lives in `fuel_daily_service.py`; and the
deterministic summary calculation lives in `fuel_summary_service.py`.
The `fuel.py` functions remain compatibility wrappers and pass the original
provider functions, cache objects, constants and shared lock into these
services.

The daily service preserves the prefetch-before-lock boundary, the lock scope
around refills/model/row generation, the house-model prediction and residual
fields, telemetry aggregation and exact response keys. The summary service
preserves both calculation lock sections, the separate counter/period lock
callbacks, AI shadow-only failure isolation, all-history calibration versus
recent display periods, estimate precedence, planning/forecast assembly and
the final summary-cache update. Summary background-worker lifecycle,
stale-while-refresh behavior, cold-start wait and cache TTL remain in
`fuel.py` and are not yet extracted.

New contract tests cover daily validation, payload/prediction behavior,
prefetch failure propagation, summary payload/cache behavior, unavailable
daily data and AI failure isolation. Docker and CI include both service modules.
Historical ThingSpeak replay and old/new parity remain mandatory before any
runtime cutover.


## Refill command service extraction

`fuel_refill_service.py` now owns add-refill and void-refill validation and
orchestration. The public API facade still owns token authentication and JSON
decoding; the service receives the original writer, raw refill reader, timezone,
bag size and error type as dependencies. Regression coverage includes kg and
bag input, naive local timestamps, note truncation, invalid ranges/formats,
voiding one raw entry and the 400/404 contract. The module is included in the
API Docker image and ThermoFlow CI. No refill writes were performed during
this refactor.


## Summary runtime and refill command service extraction

The summary worker lifecycle now lives in `fuel_summary_runtime.py`. The
compatibility facade supplies the same shared worker state, guard, summary
cache, prefetch/build callbacks, thread factory, monotonic clock and existing
60-second cache / 25-second cold-start limits. The extracted runtime preserves
single-worker behavior, stale-while-refresh responses, cold-start warming
payloads, HTTP 502 mapping for completed worker failures, and warmup only when
the refill channel is configured.

Refill command validation and orchestration lives in `fuel_refill_service.py`.
The facade continues to own HTTP authentication and JSON decoding; the service
preserves kg/bag conversion, accepted range, ISO timestamps, Warsaw timezone
for naive values, note truncation and single raw-entry void semantics. The
ThingSpeak writer remains responsible for UTC write timestamps, 503/429
translation and the existing cache invalidation set.

Both modules are packaged in the API image and covered by CI. The latest
validation run for the preceding code head passed Python regression, frontend
regression and Docker builds for linux/amd64 and linux/arm64. No merge,
deployment, controller write or production engine switch was performed.
