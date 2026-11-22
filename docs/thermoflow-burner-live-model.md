# ThermoFlow Burner ML — model artifact gate

This phase turns an already evaluated offline Burner ML benchmark into a small,
versioned artifact that can later be consumed by a live **read-only shadow**
runtime. It does not enable live predictions by itself and does not change the
existing controller, fuel estimator, or return-temperature protection.

## Build

First produce a raw FIRE snapshot and the observed-cycle report using the
existing offline tools. Then evaluate the benchmark:

```bash
python thermoflow_observed_cycles.py fire-snapshot.json > observed-cycles.json
python thermoflow_burner_ml.py observed-cycles.json > burner-benchmark.json
python thermoflow_burner_live.py burner-benchmark.json > burner-model-artifact.json
```

The final command exits non-zero unless the benchmark is evaluated and its
candidate beats **both** the training-median and previous-cycle baselines on
the untouched chronological holdout. It exports only the inference parameters,
sample counts, aggregate holdout metrics, and source snapshot digest. It does
not export training rows, holdout rows, or raw telemetry.

## Artifact and inference contract

- Schema: `thermoflow-burner-live-model/1`.
- Features available at the start of a FIRE-on cycle:
  - `previous_duration_minutes`: immediately preceding complete FIRE-confirmed cycle.
  - `idle_minutes`: elapsed time since that cycle ended.
- Target: duration of the current complete FIRE-confirmed cycle, in minutes.
- Only the existing ordinary-least-squares model and exact feature contract are accepted.
- Minimum evidence: 20 training samples and 5 untouched holdout samples.
- Artifact maximum size: 16 KiB; maximum age: 30 days; future clock skew: 5 minutes.
- A model that does not beat both baselines cannot be exported.
- Artifact state is marked `manual_review_required`; this is not automatic model
  promotion and does not imply that the model is suitable for operational use.

The `load_live_artifact` function validates schema, feature dimensions, finite
coefficients, sample counts, and freshness. `predict_cycle_duration` accepts
only the two cycle-start features and returns an estimate in minutes.

## Safety boundary

This is inference plumbing only. It does not infer ignition from unrelated
signals, estimate pellet mass, write telemetry, persist files, alter setpoints,
operate pumps or valves, or communicate with the boiler controller. The
55°C ±1°C return-protection requirement remains unchanged. A later runtime
integration must keep predictions advisory, expose stale/collecting states,
and fail closed when no valid artifact is configured.
