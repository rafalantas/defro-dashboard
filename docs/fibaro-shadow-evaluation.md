# Fibaro shadow backtesting and readiness

This stage adapts the existing `thermoflow_shadow_evaluation.py` ledger
evaluator for the Fibaro next-day event-count model.

## Backtest contract

- Evaluates only the newest chronological holdout emitted by PR #141.
- Converts local day labels into UTC decision boundaries for the ledger; each
  daily target becomes available only at the end of that target day.
- Records candidate and training-median baseline predictions with explicit
  decision time and data cutoff.
- Uses one-day, non-overlapping windows and verified labels.
- Reports per-window metrics, paired coverage, aggregate MAE, and whether the
  candidate beats the training-only median baseline.

## Readiness

`ready_for_human_review` requires at least five holdout days, complete paired
labels, and lower candidate MAE than the baseline. It is not model approval.
Automatic promotion is always disabled. No artifact is persisted or wired
into live inference.

No HC3 write, thermostat or schedule change, boiler/pump/valve control,
fuel-forecast change, or return-protection change is performed.
