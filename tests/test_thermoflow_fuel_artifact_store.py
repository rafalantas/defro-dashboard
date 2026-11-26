"""Regression tests for verified Fuel ML SQLite artifact persistence."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thermoflow_fuel_artifact_store import (
    load_fuel_model_artifact, persist_fuel_shadow_result,
)
from thermoflow_fuel_live import FEATURE_KEYS, TARGET_KEY
from thermoflow_store import SQLiteStore

NOW = 1_800_000_000


def fixture():
    first = NOW - 26 * 86400
    periods = []
    for index in range(26):
        start = first + index * 86400
        end = start + 86400
        periods.append({
            "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "refilled_kg": 10.0,
            "baseline_kg_per_day": 12.0,
            "baseline_issued_at": datetime.fromtimestamp(start - 300, timezone.utc).isoformat(),
            "mass_reference": "verified_equal_fill_level",
        })
    snapshot = {
        "schema": "thermoflow-verified-fuel-periods/1",
        "as_of": datetime.fromtimestamp(periods[-1]["end"] and first + 26 * 86400,
                                         timezone.utc).isoformat(),
        "baseline_mode": "as_issued",
        "label_mode": "verified_equal_fill_level",
        "calibration_epochs": [],
        "periods": periods,
    }
    training, holdout = [], []
    for index in range(1, 26):
        start = first + index * 86400
        row = {
            "timestamp": float(start),
            "complete": True,
            "features": {"baseline_kg_per_day": 12.0, "previous_kg_per_day": 10.0},
            "target": 10.0,
        }
        (training if index <= 20 else holdout).append(row)
    dataset = {
        "status": "ready_for_shadow_training",
        "feature_keys": list(FEATURE_KEYS), "target_key": TARGET_KEY,
        "eligible_samples": 25, "training_samples": 20, "holdout_samples": 5,
        "training": training, "holdout": holdout,
    }
    model = {
        "model_schema_version": "1.0.0", "dataset_schema_version": "1.0.0",
        "algorithm": "ordinary_least_squares", "feature_keys": list(FEATURE_KEYS),
        "target_key": TARGET_KEY, "feature_means": [12.0, 10.0],
        "feature_scales": [1.0, 1.0], "intercept": 10.0,
        "coefficients": [0.0, 0.0], "training_samples": 20,
        "holdout_samples": 5, "mode": "read_only_shadow",
    }
    benchmark = {
        "schema": "thermoflow-fuel-shadow/1", "status": "evaluated",
        "mode": "offline_shadow_only", "promotion": "disabled",
        "baseline_mode": "as_issued", "label_mode": "verified_equal_fill_level",
        "dataset": dataset, "model": model,
        "evaluation": {
            "as_issued_baseline": {"samples": 5, "mae_kg_per_day": 2.0, "rmse_kg_per_day": 2.0, "bias_kg_per_day": 0.0},
            "previous_period": {"samples": 5, "mae_kg_per_day": 1.5, "rmse_kg_per_day": 1.5, "bias_kg_per_day": 0.0},
            "candidate": {"samples": 5, "mae_kg_per_day": 1.0, "rmse_kg_per_day": 1.0, "bias_kg_per_day": 0.0,
                          "beats_as_issued_baseline_on_mae": True},
        },
    }
    return snapshot, benchmark, first + 26 * 86400 + 10


class FuelArtifactStoreTests(unittest.TestCase):
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
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("feature_windows", "datasets", "model_versions",
                                  "model_evaluations", "shadow_predictions")}

    def test_persists_verified_dataset_model_metrics_and_holdout_predictions(self):
        snapshot, benchmark, clock = fixture()
        result = persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock)
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(result["inserted"]["feature_windows"], 25)
        self.assertEqual(result["inserted"]["model_evaluations"], 1)
        self.assertEqual(result["inserted"]["shadow_predictions"], 5)
        self.assertEqual(self.counts(), {
            "feature_windows": 25, "datasets": 1, "model_versions": 1,
            "model_evaluations": 1, "shadow_predictions": 5,
        })
        loaded = load_fuel_model_artifact(
            self.store, model_version=result["model_version"], now=clock + 10
        )
        self.assertEqual(loaded["model_name"], "fuel_rate")
        self.assertEqual(loaded["artifact"]["latest_verified_rate_kg_per_day"], 10.0)

    def test_reimport_is_idempotent(self):
        snapshot, benchmark, clock = fixture()
        first = persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock)
        second = persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock + 1)
        self.assertEqual(first["dataset_version"], second["dataset_version"])
        self.assertEqual(second["inserted"]["feature_windows"], 0)
        self.assertEqual(second["inserted"]["shadow_predictions"], 0)
        self.assertEqual(self.counts()["shadow_predictions"], 5)

    def test_rejects_feature_not_backed_by_adjacent_verified_period(self):
        snapshot, benchmark, clock = fixture()
        benchmark["dataset"]["training"][0]["features"]["previous_kg_per_day"] = 99.0
        with self.assertRaisesRegex(ValueError, "adjacent verified period"):
            persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock)
        self.assertEqual(self.counts()["datasets"], 0)

    def test_non_evaluated_benchmark_is_skipped_without_writes(self):
        snapshot, benchmark, clock = fixture()
        benchmark["status"] = "collecting"
        result = persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.counts()["datasets"], 0)

    def test_loader_rejects_modified_artifact_metadata(self):
        snapshot, benchmark, clock = fixture()
        result = persist_fuel_shadow_result(self.store, snapshot, benchmark, recorded_at=clock)
        with self.store.connection() as db:
            row = db.execute("SELECT metadata_json FROM model_versions WHERE model_name='fuel_rate'").fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["model_artifact"]["model"]["intercept"] = 999.0
            db.execute("UPDATE model_versions SET metadata_json=? WHERE model_name='fuel_rate'",
                       (json.dumps(metadata),))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            load_fuel_model_artifact(self.store, model_version=result["model_version"], now=clock + 10)


if __name__ == "__main__":
    unittest.main()
