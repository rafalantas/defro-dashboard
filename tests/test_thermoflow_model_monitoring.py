"""Legacy pure-monitor tests plus SQLite-backed model monitoring tests."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_model_monitoring import monitor_models, monitor_sqlite_models
from thermoflow_store import SQLiteStore

NOW = 1_810_000_000


def ledger(candidate_offset=0.5, coverage=True):
    cases = []
    for i, actual in enumerate((10., 11., 12., 13.)):
        decision = NOW - 1000 + i * 100
        outcome = decision + 30
        cases.append({"id":str(i), "decision_at":decision, "outcome_end":outcome,
            "label":{"verified":coverage, "available_at":outcome+1, "value":actual},
            "baseline":{"issued_at":decision, "data_cutoff":decision-1, "value":actual+1},
            "candidate":{"issued_at":decision, "data_cutoff":decision-1, "value":actual+candidate_offset}})
    return {"schema":"thermoflow-shadow-ledger/1", "task":"fuel-kg-per-day",
        "kind":"regression", "start":NOW-1000, "end":NOW-500, "as_of":NOW,
        "window_seconds":125, "cases":cases}


class ModelMonitoringTests(unittest.TestCase):
    def test_missing_artifact_and_ledger_are_collecting_not_healthy(self):
        result = monitor_models(now=NOW)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "missing")
        self.assertIn("shadow_ledger_missing", result["modules"]["fuel_ml"]["alerts"])

    def test_stale_and_future_artifacts_are_rejected(self):
        result = monitor_models(now=NOW, artifacts={
            "burner_ml":{"created_at":NOW-40*86400},
            "fuel_ml":{"created_at":NOW+60}})
        self.assertEqual(result["modules"]["burner_ml"]["status"], "stale")
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "invalid")

    def test_error_regression_triggers_review_without_promotion(self):
        result = monitor_models(now=NOW,
            artifacts={"fuel_ml":{"created_at":NOW-60}},
            ledgers={"fuel_ml":ledger(candidate_offset=3.0)})
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "review")
        self.assertIn("candidate_error_regression", result["modules"]["fuel_ml"]["alerts"])
        self.assertEqual(result["promotion"], "disabled")

    def test_low_coverage_is_explicit(self):
        result = monitor_models(now=NOW,
            artifacts={"fuel_ml":{"created_at":NOW-60}},
            ledgers={"fuel_ml":ledger(coverage=False)})
        self.assertEqual(result["modules"]["fuel_ml"]["status"], "collecting")

    def test_monitor_does_not_call_missing_data_good(self):
        result = monitor_models(now=NOW, artifacts={"burner_ml":{"created_at":NOW-5}})
        self.assertIn("shadow_ledger_missing", result["modules"]["burner_ml"]["alerts"])
        self.assertEqual(result["modules"]["burner_ml"]["status"], "collecting")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class SQLiteModelMonitoringTests(unittest.TestCase):
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

    def insert_fuel_model(self, *, corrupt=False, predictions=True):
        artifact = {"created_at": NOW - 60, "mode": "read_only_shadow"}
        digest = hashlib.sha256(canonical(artifact).encode("utf-8")).hexdigest()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO model_versions(model_name,version,created_at,artifact_path,"
                "artifact_sha256,metadata_json,status) VALUES(?,?,?,?,?,?,?)",
                ("fuel_rate", "1.0.0+test", NOW - 60, None,
                 "0" * 64 if corrupt else digest,
                 canonical({"model_artifact": artifact, "model_artifact_sha256": digest}), "shadow"),
            )
            if predictions:
                decision, outcome = NOW - 3600, NOW - 1800
                db.execute(
                    "INSERT INTO shadow_predictions(model_name,model_version,entity_key,"
                    "predicted_at,target_at,prediction_json,actual_json,evaluation_status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("fuel_rate", "1.0.0+test", f"refill-period:{decision}",
                     NOW - 10, outcome,
                     canonical({"value": 11.0, "features": {"baseline_kg_per_day": 12.0}}),
                     canonical({"value": 10.0, "verified": True}), "evaluated"),
                )

    def test_reads_sqlite_artifact_and_verified_shadow_outcome(self):
        self.insert_fuel_model()
        result = monitor_sqlite_models(self.store, now=NOW)
        fuel = result["modules"]["fuel_ml"]
        self.assertEqual(result["status"], "collecting")  # Burner model is not registered in this fixture.
        self.assertEqual(fuel["status"], "observing")
        self.assertEqual(fuel["evaluation"]["status"], "evaluated")
        self.assertEqual(fuel["evaluation"]["windows"][0]["paired_labeled"], 1)
        self.assertEqual(fuel["alerts"], [])

    def test_checksum_mismatch_is_reported_as_invalid(self):
        self.insert_fuel_model(corrupt=True)
        result = monitor_sqlite_models(self.store, now=NOW)
        fuel = result["modules"]["fuel_ml"]
        self.assertEqual(fuel["status"], "invalid")
        self.assertIn("artifact_checksum_mismatch", fuel["alerts"])


    def insert_fibaro_model(self, *, quality_status="ready", with_outcome=True):
        artifact = {
            "model_schema_version": "1.0.0",
            "mode": "offline_shadow_only",
        }
        digest = hashlib.sha256(canonical(artifact).encode("utf-8")).hexdigest()
        version = "1.0.0+" + digest[:16]
        metadata = {
            "schema": "thermoflow-fibaro-artifact-store/1",
            "mode": "offline_shadow_only",
            "model_artifact": artifact,
            "model_artifact_sha256": digest,
            "automatic_promotion": False,
        }
        metrics = {
            "schema": "thermoflow-fibaro-artifact-store/1",
            "quality_status": quality_status,
            "backtest_summary": {"holdout_samples": 5},
            "readiness": {"status": "ready" if quality_status == "ready" else "collecting"},
        }
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO model_versions(model_name,version,created_at,artifact_path,"
                "artifact_sha256,metadata_json,status) VALUES(?,?,?,?,?,?,?)",
                ("fibaro_daily_heat_demand", version, NOW - 60, None,
                 digest, canonical(metadata), "shadow"),
            )
            db.execute(
                "INSERT INTO model_evaluations(model_name,model_version,dataset_version,"
                "evaluated_at,metrics_json,status) VALUES(?,?,?,?,?,?)",
                ("fibaro_daily_heat_demand", version, "dataset-test", NOW - 30,
                 canonical(metrics), "evaluated"),
            )
            if with_outcome:
                db.execute(
                    "INSERT INTO shadow_predictions(model_name,model_version,entity_key,"
                    "predicted_at,target_at,prediction_json,actual_json,evaluation_status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("fibaro_daily_heat_demand", version, "local-day:2026-11-20",
                     NOW - 100, NOW - 50, canonical({"value": 4.0}),
                     canonical({"value": 3.0, "verified": True}), "evaluated"),
                )

    def test_reads_fibaro_artifact_evaluation_and_verified_outcomes(self):
        self.insert_fibaro_model()
        result = monitor_sqlite_models(self.store, now=NOW)
        fibaro = result["modules"]["fibaro_daily_ml"]
        self.assertEqual(fibaro["status"], "observing")
        self.assertEqual(fibaro["outcomes"]["evaluated"], 1)
        self.assertEqual(fibaro["evaluation"]["quality_status"], "ready")
        self.assertEqual(fibaro["alerts"], [])

    def test_fibaro_quality_gate_not_ready_stays_collecting(self):
        self.insert_fibaro_model(quality_status="collecting")
        result = monitor_sqlite_models(self.store, now=NOW)
        fibaro = result["modules"]["fibaro_daily_ml"]
        self.assertEqual(fibaro["status"], "collecting")
        self.assertIn("fibaro_quality_gate_not_ready", fibaro["alerts"])

    def test_pending_prediction_does_not_count_as_verified_coverage(self):
        self.insert_fuel_model(predictions=False)
        result = monitor_sqlite_models(self.store, now=NOW)
        fuel = result["modules"]["fuel_ml"]
        self.assertEqual(fuel["status"], "collecting")
        self.assertIn("shadow_ledger_missing", fuel["alerts"])

if __name__ == "__main__":
    unittest.main()
