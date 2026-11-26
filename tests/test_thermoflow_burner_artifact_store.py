"""Tests for explicit, idempotent Burner ML persistence in shared SQLite."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_burner_artifact_store import (
    load_burner_model_artifact,
    persist_burner_shadow_result,
)
from thermoflow_burner_live import build_live_artifact
from thermoflow_ml_burner_adapter import FEATURE_KEYS, TARGET_KEY
from thermoflow_store import SQLiteStore

NOW = 1800000000
SOURCE_SHA = "a" * 64


def inputs():
    training = []
    holdout = []
    for index in range(25):
        row = {
            "timestamp": float(NOW - 100000 + index * 1000),
            "target": 80.0 + index,
            "features": {
                "previous_duration_minutes": 75.0 + index,
                "idle_minutes": 20.0 + index / 2,
            },
        }
        (training if index < 20 else holdout).append(row)
    model = {
        "model_schema_version": "1.0.0",
        "dataset_schema_version": "1.0.0",
        "algorithm": "ordinary_least_squares",
        "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY,
        "feature_means": [84.5, 24.75],
        "feature_scales": [5.766281297335398, 2.883140648667699],
        "intercept": 90.0,
        "coefficients": [-1.0, -0.5],
        "training_samples": 20,
        "holdout_samples": 5,
        "mode": "read_only_shadow",
        "safety": "trained_on_training_partition_only_no_production_effect",
    }
    evaluation = {
        "training_samples": 20,
        "holdout_samples": 5,
        "training_ends_at": training[-1]["timestamp"],
        "holdout_starts_at": holdout[0]["timestamp"],
        "baselines": {
            "training_median": {"samples": 5, "mae_minutes": 50.0,
                                "rmse_minutes": 55.0, "bias_minutes": -40.0},
            "previous_cycle": {"samples": 5, "mae_minutes": 60.0,
                               "rmse_minutes": 65.0, "bias_minutes": 30.0},
        },
        "candidate": {
            "algorithm": "ordinary_least_squares", "samples": 5,
            "mae_minutes": 20.0, "rmse_minutes": 22.0,
            "bias_minutes": -10.0, "beats_both_baselines_on_mae": True,
        },
    }
    benchmark = {
        "schema": "thermoflow-burner-shadow/1",
        "status": "evaluated",
        "source_snapshot_sha256": SOURCE_SHA,
        "mode": "offline_shadow_only",
        "promotion": "disabled",
        "eligible_samples": 25,
        "model": model,
        "evaluation": evaluation,
    }
    artifact = build_live_artifact(benchmark, created_at=NOW)
    report = {
        "schema": "thermoflow-observed-cycles/1",
        "input_sha256": SOURCE_SHA,
        "dataset": {
            "schema_version": "1.0.0",
            "status": "ready_for_shadow_training",
            "feature_keys": list(FEATURE_KEYS),
            "target_key": TARGET_KEY,
            "eligible_samples": 25,
            "training_samples": 20,
            "holdout_samples": 5,
            "chronology": "oldest_training_newest_holdout",
            "training": training,
            "holdout": holdout,
        },
    }
    return report, benchmark, artifact


class BurnerArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()

    def tearDown(self):
        self.temp.cleanup()

    def counts(self):
        with self.store.connection() as db:
            return {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("feature_windows", "datasets", "model_versions",
                              "model_evaluations", "shadow_predictions")
            }

    def test_persists_dataset_model_evaluation_and_holdout(self):
        report, benchmark, artifact = inputs()
        result = persist_burner_shadow_result(
            self.store, report, benchmark, artifact, recorded_at=NOW + 10
        )
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(result["inserted"]["feature_windows"], 25)
        self.assertEqual(result["inserted"]["model_evaluations"], 1)
        self.assertEqual(result["inserted"]["shadow_predictions"], 5)
        self.assertEqual(self.counts(), {
            "feature_windows": 25, "datasets": 1, "model_versions": 1,
            "model_evaluations": 1, "shadow_predictions": 5,
        })

    def test_reimport_is_idempotent(self):
        report, benchmark, artifact = inputs()
        first = persist_burner_shadow_result(
            self.store, report, benchmark, artifact, recorded_at=NOW + 10
        )
        second = persist_burner_shadow_result(
            self.store, report, benchmark, artifact, recorded_at=NOW + 20
        )
        self.assertEqual(first["inserted"]["feature_windows"], 25)
        self.assertEqual(second["inserted"]["feature_windows"], 0)
        self.assertEqual(second["inserted"]["model_evaluations"], 0)
        self.assertEqual(second["inserted"]["shadow_predictions"], 0)
        self.assertEqual(self.counts()["shadow_predictions"], 5)

    def test_load_checks_model_hash_and_schema(self):
        report, benchmark, artifact = inputs()
        saved = persist_burner_shadow_result(
            self.store, report, benchmark, artifact, recorded_at=NOW + 10
        )
        loaded = load_burner_model_artifact(
            self.store, model_version=saved["model_version"], now=NOW + 20
        )
        self.assertEqual(loaded["artifact"], artifact)
        self.assertEqual(loaded["model_name"], "burner_cycle_duration")

        with self.store.connection() as db:
            row = db.execute(
                "SELECT metadata_json FROM model_versions WHERE model_name=?",
                ("burner_cycle_duration",),
            ).fetchone()
            import json
            metadata = json.loads(row["metadata_json"])
            metadata["model_artifact"]["model"]["intercept"] = 999
            db.execute(
                "UPDATE model_versions SET metadata_json=? WHERE model_name=?",
                (json.dumps(metadata), "burner_cycle_duration"),
            )
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            load_burner_model_artifact(
                self.store, model_version=saved["model_version"], now=NOW + 20
            )

    def test_rejects_artifact_from_different_snapshot(self):
        report, benchmark, artifact = inputs()
        artifact["source_snapshot_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "source snapshot"):
            persist_burner_shadow_result(
                self.store, report, benchmark, artifact, recorded_at=NOW + 10
            )

    def test_non_evaluated_benchmark_is_not_persisted(self):
        report, benchmark, artifact = inputs()
        benchmark["status"] = "collecting"
        result = persist_burner_shadow_result(
            self.store, report, benchmark, artifact, recorded_at=NOW + 10
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.counts()["datasets"], 0)


if __name__ == "__main__":
    unittest.main()
