# ThermoFlow Model Monitoring & Drift Detection

The offline monitor consumes versioned model artifact metadata and frozen shadow
ledgers. It checks artifact age/future timestamps, missing evidence, paired
coverage, candidate-vs-baseline MAE regression, and first-to-last chronological
error drift. Regression flags are configurable review thresholds, not claims
that a model caused a change.

Statuses distinguish missing, invalid, stale, collecting, limited coverage,
observing and review. Missing labels, no ledger, or no paired holdout cannot
produce a healthy status. Alert-rate metrics with zero denominators remain null.

Run as an offline report from a trusted operator process. Keep ledgers and
artifacts outside source control. Monitoring never trains, retrains, promotes,
or replaces a model. A review alert requires human inspection and cannot alter
the boiler, pumps, valve, feeder, ignition or STB.


Aggregated window metrics are weighted by the number of paired labeled cases; keep window boundaries chronological and non-overlapping.


## Fibaro daily-demand model

The SQLite monitor also reports the persisted `fibaro_daily_heat_demand`
artifact, its latest evaluation/quality-gate status, and the number of verified,
pending, and total shadow holdout outcomes. It does not invent a baseline
comparison for Fibaro: this model currently stores event-count predictions but
does not persist a comparable baseline for every holdout row. A fresh artifact
without a completed quality-gated evaluation or verified outcomes remains
`collecting`; pending outcomes are reported as limited coverage.
