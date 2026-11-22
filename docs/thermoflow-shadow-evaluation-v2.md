# ThermoFlow Shadow Evaluation 2.0

Run offline:

    python3 -m thermoflow_shadow_evaluation_v2 shadow-ledger.json > shadow-report-v2.json

The v2 evaluator wraps the strict thermoflow-shadow-ledger/1 contract and adds
global paired metrics, verified-label and paired coverage, invalid prediction
counts, and first-to-last chronological window comparisons. Regression tasks
report MAE, RMSE and signed bias. Alert tasks report false positive/negative
rates with null denominators when no verified negatives or positives exist.

Only cases with a verified label and valid baseline and candidate predictions
issued by decision time are included in paired metrics. Missing labels reduce
coverage; they are never treated as zero or negative. Original per-window
output is retained.

The evaluator is offline and read-only. It does not train, promote, or write
models. Synthetic fixtures prove evaluator mechanics only. Timestamp and
schema checks cannot independently prove that a label was physically verified
or that historical data provenance is genuine; that remains an operator task.
