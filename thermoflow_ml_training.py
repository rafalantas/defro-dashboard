"""Deterministic, dependency-free shadow ML training and evaluation.

The trainer consumes only a validated chronological dataset. It fits ordinary
least squares on the training partition and evaluates once on the untouched
holdout. It never fetches telemetry, persists artifacts, or affects production.
"""
from __future__ import annotations

from math import isfinite, sqrt
from statistics import median

MODEL_SCHEMA_VERSION = "1.0.0"
SUPPORTED_DATASET_SCHEMA = "1.0.0"
MIN_TRAINING_SAMPLES = 20
MIN_HOLDOUT_SAMPLES = 5


def _finite_number(value) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("dataset contains a non-finite value")
    return number


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small linear system using Gaussian elimination with pivoting."""
    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-12:
            raise ValueError("features are linearly dependent; model is not identifiable")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


def _predict_row(model: dict, features: dict) -> float:
    keys = model["feature_keys"]
    values = [_finite_number(features[key]) for key in keys]
    standardized = [
        (value - model["feature_means"][index]) / model["feature_scales"][index]
        for index, value in enumerate(values)
    ]
    return model["intercept"] + sum(
        coefficient * value
        for coefficient, value in zip(model["coefficients"], standardized)
    )


def predict(model: dict, features: dict) -> dict:
    """Run one read-only shadow prediction using a returned model artifact."""
    if not isinstance(model, dict) or model.get("model_schema_version") != MODEL_SCHEMA_VERSION:
        raise ValueError("unsupported or malformed model artifact")
    if model.get("mode") != "read_only_shadow":
        raise ValueError("model is not marked read_only_shadow")
    if not isinstance(features, dict):
        raise ValueError("features must be a mapping")
    value = _predict_row(model, features)
    return {
        "status": "predicted",
        "prediction": value,
        "target_key": model["target_key"],
        "model_schema_version": MODEL_SCHEMA_VERSION,
        "mode": "read_only_shadow",
        "safety": "prediction_only_no_production_effect",
    }


def _metrics(actual: list[float], predicted: list[float]) -> dict:
    errors = [guess - truth for truth, guess in zip(actual, predicted)]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = sqrt(sum(error * error for error in errors) / len(errors))
    return {"samples": len(actual), "mae": round(mae, 6), "rmse": round(rmse, 6)}


def train_and_evaluate(dataset: dict) -> dict:
    """Fit OLS on training rows and compare holdout performance to median baseline.

    The holdout is never used for fitting, feature scaling, or baseline selection.
    An underpowered, malformed, or non-chronological dataset is not trained.
    """
    if not isinstance(dataset, dict):
        raise ValueError("dataset must be a mapping")
    if dataset.get("schema_version") != SUPPORTED_DATASET_SCHEMA:
        raise ValueError("unsupported dataset schema version")
    if dataset.get("status") != "ready_for_shadow_training":
        return {
            "status": "collecting",
            "reason": "dataset_not_ready",
            "model": None,
            "evaluation": None,
            "mode": "read_only_shadow",
        }

    keys = dataset.get("feature_keys")
    target_key = dataset.get("target_key")
    training = dataset.get("training")
    holdout = dataset.get("holdout")
    if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
        raise ValueError("invalid feature_keys")
    if not isinstance(target_key, str) or target_key in keys:
        raise ValueError("invalid target_key")
    if not isinstance(training, list) or not isinstance(holdout, list):
        raise ValueError("training and holdout must be lists")
    if len(training) < MIN_TRAINING_SAMPLES or len(holdout) < MIN_HOLDOUT_SAMPLES:
        return {
            "status": "collecting",
            "reason": "insufficient_training_or_holdout_samples",
            "model": None,
            "evaluation": None,
            "mode": "read_only_shadow",
        }

    def parse(rows):
        parsed = []
        previous_timestamp = None
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("features"), dict):
                raise ValueError("malformed dataset row")
            timestamp = _finite_number(row.get("timestamp"))
            if timestamp <= 0 or (previous_timestamp is not None and timestamp <= previous_timestamp):
                raise ValueError("dataset rows must be strictly chronological")
            previous_timestamp = timestamp
            features = {key: _finite_number(row["features"][key]) for key in keys}
            target = _finite_number(row["target"])
            parsed.append((timestamp, features, target))
        return parsed

    train_rows = parse(training)
    holdout_rows = parse(holdout)
    if train_rows[-1][0] >= holdout_rows[0][0]:
        raise ValueError("training and holdout partitions overlap or are out of order")

    means = []
    scales = []
    for key in keys:
        values = [row[1][key] for row in train_rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        scale = sqrt(variance) or 1.0
        means.append(mean)
        scales.append(scale)

    # Build normal equations for an intercept plus standardized features.
    width = len(keys) + 1
    gram = [[0.0] * width for _ in range(width)]
    cross = [0.0] * width
    for _timestamp, features, target in train_rows:
        vector = [1.0] + [
            (features[key] - means[index]) / scales[index]
            for index, key in enumerate(keys)
        ]
        for row in range(width):
            cross[row] += vector[row] * target
            for column in range(width):
                gram[row][column] += vector[row] * vector[column]
    parameters = _solve(gram, cross)
    model = {
        "model_schema_version": MODEL_SCHEMA_VERSION,
        "dataset_schema_version": dataset["schema_version"],
        "algorithm": "ordinary_least_squares",
        "feature_keys": list(keys),
        "target_key": target_key,
        "feature_means": means,
        "feature_scales": scales,
        "intercept": parameters[0],
        "coefficients": parameters[1:],
        "training_samples": len(train_rows),
        "holdout_samples": len(holdout_rows),
        "mode": "read_only_shadow",
        "safety": "trained_on_training_partition_only_no_production_effect",
    }

    train_actual = [row[2] for row in train_rows]
    holdout_actual = [row[2] for row in holdout_rows]
    train_predictions = [_predict_row(model, row[1]) for row in train_rows]
    holdout_predictions = [_predict_row(model, row[1]) for row in holdout_rows]
    baseline_value = median(train_actual)
    baseline_predictions = [baseline_value] * len(holdout_actual)
    model_metrics = _metrics(holdout_actual, holdout_predictions)
    baseline_metrics = _metrics(holdout_actual, baseline_predictions)
    evaluation = {
        "training": _metrics(train_actual, train_predictions),
        "holdout": model_metrics,
        "baseline": {
            "algorithm": "training_target_median",
            **baseline_metrics,
        },
        "holdout_mae_improvement": round(baseline_metrics["mae"] - model_metrics["mae"], 6),
        "beats_baseline": model_metrics["mae"] < baseline_metrics["mae"],
        "holdout_is_unseen": True,
        "mode": "read_only_shadow",
    }
    return {
        "status": "evaluated",
        "model_status": "beats_baseline" if evaluation["beats_baseline"] else "baseline_not_beaten",
        "model": model,
        "evaluation": evaluation,
        "mode": "read_only_shadow",
        "safety": "evaluation_only_no_fuel_estimate_or_boiler_control_changes",
    }
