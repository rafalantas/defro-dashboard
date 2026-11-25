# Fibaro daily heating-behaviour ML (shadow only)

This stage adds a Fibaro-specific adapter over the repository's existing,
dependency-free chronological dataset and OLS trainer. It does not introduce
a second ML framework.

## Target and features

For each pair of consecutive observed local calendar days:
- Features: previous day's observed `heatRequired` starts, `windowOpened`
  opens, and mean absolute temperature-change magnitude from clean observed
  transitions.
- Target: next day's observed `heatRequired` start count.

The target is an event count, not heat-on duration, occupancy, room comfort,
fuel consumption, or a control recommendation. Empty days are not fabricated;
non-consecutive dates break the pair. Missing values are not imputed.

## Readiness and evaluation

A Fibaro quality report with schema
`thermoflow-fibaro-data-quality/1` and `status=ready` is mandatory.
Training uses the existing 20-sample minimum and a newest 5-sample holdout.
The holdout is not used for fitting, scaling, or baseline selection. Degenerate
features fail closed as unavailable.

## Safety

The model is offline and shadow-only. It is not persisted, promoted, called by
live control, or connected to HC3 commands. No thermostat/head/schedule,
boiler, pump, valve, fuel forecast, or return-protection setting is changed.
