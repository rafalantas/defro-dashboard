"""Versioned, read-only dataset contract for future ThermoFlow ML models.

This module prepares explicitly supplied, already-derived cycle samples. It does
not fetch telemetry, infer labels, impute missing values, train models, or affect
production diagnostics and fuel estimates.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Mapping, Sequence

SCHEMA_VERSION = "1.0.0"
DEFAULT_MIN_TRAINING_SAMPLES = 20
DEFAULT_HOLDOUT_SAMPLES = 5


def prepare_chronological_dataset(
    samples: Sequence[Mapping[str, object]],
    *,
    feature_keys: Sequence[str],
    target_key: str,
    timestamp_key: str = "timestamp",
    complete_key: str = "complete",
    minimum_training_samples: int = DEFAULT_MIN_TRAINING_SAMPLES,
    holdout_samples: int = DEFAULT_HOLDOUT_SAMPLES,
) -> dict:
    """Validate and split complete labeled samples without changing their values.

    Duplicate timestamps are excluded as a group. Incomplete, malformed, or
    non-finite rows are excluded and counted. No sorting-based repair, fill,
    interpolation, synthetic rows, or target derivation is performed.
    The final holdout is always the newest chronological block.
    """
    if not feature_keys or len(set(feature_keys)) != len(feature_keys):
        raise ValueError("feature_keys must be non-empty and unique")
    if target_key in feature_keys or timestamp_key in feature_keys:
        raise ValueError("target and timestamp cannot be model features")
    if minimum_training_samples < 1 or holdout_samples < 1:
        raise ValueError("training and holdout sizes must be positive")

    accepted = []
    rejected = Counter()
    timestamp_counts = Counter()

    for sample in samples or ():
        if not isinstance(sample, Mapping):
            rejected["malformed_row"] += 1
            continue
        if sample.get(complete_key) is not True:
            rejected["incomplete"] += 1
            continue
        try:
            timestamp = float(sample[timestamp_key])
            target = float(sample[target_key])
            features = {key: float(sample[key]) for key in feature_keys}
            if not isfinite(timestamp) or timestamp <= 0:
                raise ValueError("invalid timestamp")
            if not isfinite(target) or not all(isfinite(value) for value in features.values()):
                raise ValueError("non-finite value")
        except (KeyError, TypeError, ValueError, OverflowError):
            rejected["invalid_or_missing_value"] += 1
            continue

        timestamp_counts[timestamp] += 1
        accepted.append({
            "timestamp": timestamp,
            "target": target,
            "features": features,
        })

    unique = []
    for row in accepted:
        if timestamp_counts[row["timestamp"]] > 1:
            continue
        unique.append(row)
    rejected["duplicate_timestamp"] += sum(
        1 for row in accepted if timestamp_counts[row["timestamp"]] > 1
    )
    unique.sort(key=lambda row: row["timestamp"])

    required = minimum_training_samples + holdout_samples
    ready = len(unique) >= required
    train = unique[:-holdout_samples] if ready else []
    holdout = unique[-holdout_samples:] if ready else []

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready_for_shadow_training" if ready else "collecting",
        "mode": "read_only_shadow",
        "feature_keys": list(feature_keys),
        "target_key": target_key,
        "timestamp_key": timestamp_key,
        "eligible_samples": len(unique),
        "required_samples": required,
        "training_samples": len(train),
        "holdout_samples": len(holdout),
        "training": train,
        "holdout": holdout,
        "rejected_samples": dict(sorted(rejected.items())),
        "chronology": "oldest_training_newest_holdout",
        "missing_data_policy": "reject_row_no_imputation_no_interpolation",
        "safety": "dataset_preparation_only_no_model_training_no_production_effect",
    }
