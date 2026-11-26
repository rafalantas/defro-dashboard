# ThermoFlow rolling-origin ML validation

The thermoflow_ml_validation module evaluates shadow models on multiple chronological holdout blocks. Each fold expands the training history and tests only on the immediately following unseen block. The holdout is never used for fitting, feature scaling, or baseline selection.

Defaults require at least 40 initial training rows, three consecutive holdout blocks of five rows (55 eligible samples total), at least 60 days between the first and last eligible observation, and observations in at least three UTC calendar months. These gates prevent a large but temporally narrow dataset from being mistaken for seasonal evidence.

Example:

```python
from thermoflow_ml_validation import validate_rolling_origin

result = validate_rolling_origin(
    samples,
    feature_keys=("outside_temp", "mean_return"),
    target_key="heat_required_transitions",
)
```

The result reports per-fold and aggregate MAE/RMSE against a median-of-training baseline, coverage gates, and rejected-row counts. A candidate that does not beat the baseline remains a failed benchmark; insufficient temporal coverage returns collecting. The module does not produce a promoted artifact, persist a model, retrain from HTTP, or change fuel estimates or boiler control.
