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
