"""Tests for compact, idempotent Fibaro artifact persistence."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_artifact_store import persist_fibaro_shadow_result
from thermoflow_store import SQLiteStore


def result():
    model = {
        "model_schema_version": "1.0.0",
        "mode": "read_only_shadow",
        "feature_keys": ["previous_day_heat_starts"],
        "target_key": "next_day_heat_starts",
        "feature_means": [1.0],
        "feature_scales": [1.0],
        "intercept": 2.0,
        "coefficients": [0.5],
    }
    holdout = [{
        "timestamp": 20260127,
        "features": {"previous_day_heat_starts": 2.0},
        "target": 3.0,
    }]
    return {
        "schema": "thermoflow-fibaro-shadow-pipeline/1",
        "status": "evaluated",
        "window": {"start": 1767222000, "end": 1769814000, "timezone": "Europe/Warsaw"},
        "quality": {"schema": "quality/1", "status": "ready"},
        "daily_features": [
            {"date": "2026-01-01", "heat_demand_started": 1,
             "window_opened": 0, "temperature_mean_absolute_delta_c": 0.2},
            {"date": "2026-01-02", "heat_demand_started": 0,
             "window_opened": 1, "temperature_mean_absolute_delta_c": 0.3},
        ],
        "model": {
            "status": "evaluated",
            "model": model,
            "dataset": {"holdout": holdout},
            "evaluation": {"holdout_is_unseen": True},
        },
        "backtest": {
            "status": "evaluated",
            "summary": {"holdout_days": 1, "candidate_mae": 0.0},
            "readiness": {"status": "ready_for_human_review"},
        },
    }


class FibaroArtifactStoreTests(unittest.TestCase):
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

    def test_persists_compact_features_manifest_model_metadata_and_predictions(self):
        saved = persist_fibaro_shadow_result(self.store, result(), recorded_at=1800000000)
        self.assertEqual(saved["status"], "persisted")
        self.assertEqual(saved["inserted"], {
            "feature_windows": 2, "model_evaluations": 1, "shadow_predictions": 1
        })
        self.assertFalse(saved["model_binary_stored"])
        self.assertFalse(saved["raw_source_history_duplicated"])
        self.assertEqual(self.counts(), {
            "feature_windows": 2, "datasets": 1, "model_versions": 1,
            "model_evaluations": 1, "shadow_predictions": 1,
        })

    def test_repeated_result_is_idempotent(self):
        first = persist_fibaro_shadow_result(self.store, result(), recorded_at=1800000000)
        second = persist_fibaro_shadow_result(self.store, result(), recorded_at=1800000000)
        self.assertEqual(first["inserted"]["feature_windows"], 2)
        self.assertEqual(second["inserted"]["feature_windows"], 0)
        self.assertEqual(second["inserted"]["model_evaluations"], 0)
        self.assertEqual(second["inserted"]["shadow_predictions"], 0)
        self.assertEqual(self.counts()["feature_windows"], 2)

    def test_non_evaluated_result_is_not_persisted(self):
        payload = result()
        payload["status"] = "collecting"
        saved = persist_fibaro_shadow_result(self.store, payload, recorded_at=1800000000)
        self.assertEqual(saved["status"], "skipped")
        self.assertEqual(self.counts()["feature_windows"], 0)

    def test_invalid_timezone_is_rejected(self):
        payload = result()
        payload["window"]["timezone"] = "Not/AZone"
        with self.assertRaises(ValueError):
            persist_fibaro_shadow_result(self.store, payload, recorded_at=1800000000)


if __name__ == "__main__":
    unittest.main()
