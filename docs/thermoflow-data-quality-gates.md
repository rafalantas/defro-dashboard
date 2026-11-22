# ThermoFlow Data Quality Gates

Run the offline gate against already-exported snapshots from a trusted operator
workflow. The Python API exposes inspect_fire_snapshot, inspect_fuel_snapshot,
inspect_weather_forecasts and build_data_quality_report for use by import tools.

FIRE is ready only with binary, unique, continuous samples and at least two
observed transitions. A gap over 75 seconds or duplicate timestamp blocks
cycle evidence. This is input quality, not proof of sensor correctness.

Fuel is blocked unless the snapshot explicitly carries verified equal-fill
labels and as-issued baselines. Periods must be chronological and non-overlap;
mass and baseline rate must be positive and finite; baseline issue time must
not be later than period start. Refill IDs can be checked against a manually
reviewed allowlist. An empty allowlist does not independently certify physical
provenance: operator review remains required.

Weather Impact requires an archived forecast for the exact fuel period, with
kind=forecast and available_at no later than period start. Missing forecasts
remain collecting; observations or hindsight reanalysis cannot substitute.

All results are offline, read-only and advisory. No label is fabricated, no
gap is interpolated, and no model is trained or promoted.


Quality gate output is a screening aid; the operator remains responsible for confirming physical provenance before model use.
