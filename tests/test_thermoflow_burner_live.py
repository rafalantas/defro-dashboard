import json
import unittest

from thermoflow_burner_live import (
    ARTIFACT_SCHEMA, MAX_ARTIFACT_AGE_SECONDS, build_live_artifact,
    load_live_artifact, predict_cycle_duration,
)

NOW = 1_810_000_000


def benchmark():
    return {
        "schema": "thermoflow-burner-shadow/1",
        "status": "evaluated",
        "source_snapshot_sha256": "a" * 64,
        "model": {
            "model_schema_version": "1.0.0",
            "dataset_schema_version": "1.0.0",
            "algorithm": "ordinary_least_squares",
            "feature_keys": ["previous_duration_minutes", "idle_minutes"],
            "target_key": "duration_minutes",
            "feature_means": [30.0, 60.0],
            "feature_scales": [5.0, 20.0],
            "intercept": 35.0,
            "coefficients": [2.0, -1.0],
            "training_samples": 30,
            "holdout_samples": 5,
            "mode": "read_only_shadow",
        },
        "evaluation": {
            "holdout_samples": 5,
            "candidate": {
                "samples": 5, "mae_minutes": 1.2, "rmse_minutes": 1.5,
                "bias_minutes": 0.1, "beats_both_baselines_on_mae": True,
            },
            "baselines": {
                "training_median": {"samples": 5, "mae_minutes": 3.0},
                "previous_cycle": {"samples": 5, "mae_minutes": 2.5},
            },
        },
    }


class BurnerLiveArtifactTests(unittest.TestCase):
    def test_exports_minimal_artifact_only_after_candidate_beats_both_baselines(self):
        report = benchmark()
        artifact = build_live_artifact(report, created_at=NOW)
        self.assertEqual(artifact["schema"], ARTIFACT_SCHEMA)
        self.assertEqual(artifact["promotion"], "manual_review_required")
        self.assertEqual(artifact["source_snapshot_sha256"], "a" * 64)
        self.assertNotIn("training", json.dumps(artifact))
        self.assertNotIn("holdout_rows", json.dumps(artifact))
        self.assertEqual(artifact["model"]["training_samples"], 30)

    def test_rejects_unbenchmarked_or_baseline_losing_candidate(self):
        report = benchmark()
        report["status"] = "collecting"
        with self.assertRaisesRegex(ValueError, "not evaluated"):
            build_live_artifact(report, created_at=NOW)
        report = benchmark()
        report["evaluation"]["candidate"]["beats_both_baselines_on_mae"] = False
        with self.assertRaisesRegex(ValueError, "beat both baselines"):
            build_live_artifact(report, created_at=NOW)

    def test_load_enforces_freshness_schema_and_model_dimensions(self):
        artifact = build_live_artifact(benchmark(), created_at=NOW)
        loaded = load_live_artifact(json.dumps(artifact).encode(), now=NOW + 10)
        self.assertEqual(loaded["schema"], ARTIFACT_SCHEMA)
        with self.assertRaisesRegex(ValueError, "stale"):
            load_live_artifact(artifact, now=NOW + MAX_ARTIFACT_AGE_SECONDS + 1)
        malformed = {**artifact, "model": {**artifact["model"], "coefficients": [1.0]}}
        with self.assertRaisesRegex(ValueError, "dimensions"):
            load_live_artifact(malformed, now=NOW)

    def test_rejects_future_artifact_and_oversized_input(self):
        artifact = build_live_artifact(benchmark(), created_at=NOW + 1000)
        with self.assertRaisesRegex(ValueError, "future"):
            load_live_artifact(artifact, now=NOW)
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            load_live_artifact(b" " * (16 * 1024 + 1), now=NOW)

    def test_prediction_uses_only_cycle_start_features_and_is_read_only(self):
        artifact = load_live_artifact(
            build_live_artifact(benchmark(), created_at=NOW), now=NOW
        )
        result = predict_cycle_duration(
            artifact, previous_duration_minutes=35, idle_minutes=80
        )
        self.assertEqual(result["status"], "predicted")
        self.assertEqual(result["unit"], "minutes")
        self.assertEqual(result["prediction"], 36.0)
        self.assertEqual(result["mode"], "read_only_shadow")
        with self.assertRaisesRegex(ValueError, "positive"):
            predict_cycle_duration(
                artifact, previous_duration_minutes=0, idle_minutes=80
            )


if __name__ == "__main__":
    unittest.main()
