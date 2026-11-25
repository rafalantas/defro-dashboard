# Fibaro shadow artifact storage

## What is stored

The optional `persist=True` path writes only after the persisted Fibaro evidence
passes quality gates and a chronological shadow backtest has completed.

- One row per observed local day in `feature_windows`, with a deterministic
  content-addressed dataset version.
- One small `datasets.manifest_json` record pointing to the feature-window
  version and quality-report checksum. The complete dataset is not copied into
  the manifest.
- Model schema, feature keys, target, version and SHA-256 in `model_versions`.
  The fitted model binary remains outside SQLite.
- Aggregate metrics/readiness in `model_evaluations`.
- Holdout-only prediction and verified label pairs in `shadow_predictions`.

Raw Fibaro event payloads remain in `fibaro_events`; the artifact store does not
copy them. Re-running the same evaluated result is idempotent for feature rows,
model versions, and holdout prediction keys.

## Explicit operation

The pipeline is read-only by default. A caller must pass `persist=True` to
write derived artifacts. Incomplete quality reports, collecting models, and
unevaluated backtests are skipped. No scheduler or API startup hook is installed.

## Retention

These records are evidence for reproducibility and chronological evaluation.
Do not prune them using the generic cache/collector-run retention policy.
A future retention policy must account for model versions, dataset references,
and prediction/evaluation audit requirements before removing any artifacts.
