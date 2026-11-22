# ThermoFlow weather-aware forecasting

## Scope

The Diagnostics → Unified Intelligence → Weather Impact card now exposes the
existing Fuel planner's short-range weather-aware pellet forecast. It does not
create a second weather provider or a second fuel model.

The existing planner remains responsible for:
- Open-Meteo daily forecast inputs (mean outdoor temperature, solar radiation
  and maximum wind speed);
- the calibrated house HDD model and, when eligible, the selected daily model
  with thermal lag, solar and wind terms;
- daily kg/day predictions and the existing longer-range planning based on
  five-year monthly climate normals.

The Intelligence projection intentionally uses only the planner's explicit
`forecast` rows. It does not relabel monthly climate normals as a live weather
forecast. The displayed total and average cover the available forecast rows,
up to seven days.

## API

`GET /api/thermoflow/v1/intelligence` adds `weather_forecast`:

- `status`: `ready`, `collecting`, `stale` or `unavailable`;
- `days`: validated daily temperature, solar, wind and predicted pellet kg;
- `week`: total kg, average kg/day and number of forecast days;
- `model`: selected model kind, training-day count, weather terms and
  out-of-sample LOO RMSE when the daily model provides it;
- `generated_at` and `age_seconds`: source summary freshness.

The projection rejects malformed, duplicate, non-finite and physically
implausible rows. A source summary older than six hours is marked stale. Missing
or stale data never produces a synthetic forecast.

The LOO RMSE is a historical error metric, not a statistical confidence
interval or a guarantee for the displayed forecast.

## Historical Weather Impact

The existing offline Weather Impact benchmark remains separate. It pairs
verified fuel periods with archived as-issued weather forecasts and compares
the same chronological holdout with and without the forecast-temperature
feature. It does not use later weather observations as if they were forecasts.

## Safety and data boundaries

- The forecast is advisory and read-only.
- No weather request is added to the eModul polling path; the existing Fuel
  planner owns Open-Meteo retrieval and caching.
- No new ThingSpeak writes, database, background training or artifact is added.
- No boiler, valve, pump, feeder, ignition, STB or return-protection setting is
  controlled or modified.
- Return-temperature protection remains **55°C ±1°C**.
