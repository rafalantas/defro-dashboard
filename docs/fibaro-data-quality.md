# Fibaro HC3 event-data quality gate

`thermoflow_fibaro_data_quality.py` evaluates the persisted HC3 event stream
before it can be used by a downstream shadow model.

## Checks

- Event IDs and Unix timestamps must be valid, unique, and inside the requested
  analysis window.
- Event payloads used for supported transition types must have the expected
  shape; temperature values outside -10..45 °C block eligibility.
- A non-empty zone/head snapshot is required to map head events to zones.
- Minimum event volume and observed-day thresholds default to 50 events and
  7 distinct UTC days. Below either threshold the result remains
  `collecting`, not ready.
- The existing event-history analyzer is reused for recognized transitions
  and suspected transient temperature spikes.

## Important interpretation

Fibaro history is a sparse event stream, not a fixed-rate sensor feed. Quiet
periods are not treated as missing samples, and this gate never claims full
timeline completeness or reconstructs state durations. A clean gate means
the observed records passed the stated checks; it does not prove that HC3
retained every event.

The result is advisory and shadow-only. `promotion_enabled` is always false;
it does not alter boiler control, return protection (55°C ±1°C), or the pellet
forecast.
