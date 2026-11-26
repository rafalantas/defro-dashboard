"""Regression tests for the read-only SQLite data audit."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from thermoflow_sqlite_data_audit import read_sqlite_data_audit
from thermoflow_store import SQLiteStore

NOW = 1_800_000_000


class SQLiteDataAuditTests(unittest.TestCase):
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

    def test_reports_sources_and_keeps_training_gate_closed(self):
        payload = '{"schema":"thermoflow-emodul-ml-input/1","source":"emodul"}'
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,payload_json,payload_sha256) "
                "VALUES(?,?,?,?,?,?)",
                ("emodul", "test:1", NOW - 60, NOW - 50, payload, digest),
            )
        result = read_sqlite_data_audit(self.path, now=NOW, days=30)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["sources"]["emodul_telemetry"], 1)
        self.assertEqual(result["inventory"]["telemetry_samples"]["window_count"], 1)
        self.assertEqual(result["training_readiness"]["status"], "collecting")
        self.assertFalse(result["training_readiness"]["training_eligibility"])
        self.assertTrue(result["safety"]["read_only"])
        self.assertFalse(result["safety"]["model_training"])

    def test_counts_verified_shadow_labels_without_accepting_pending(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO shadow_predictions(model_name,model_version,entity_key,predicted_at,target_at,"
                "prediction_json,actual_json,evaluation_status) VALUES(?,?,?,?,?,?,?,?)",
                ("fuel_rate", "v1", "refill:1", NOW - 120, NOW - 60,
                 '{"value":1,"features":{"baseline_kg_per_day":2}}',
                 '{"value":1.2,"verified":true}', "evaluated"),
            )
            db.execute(
                "INSERT INTO shadow_predictions(model_name,model_version,entity_key,predicted_at,target_at,"
                "prediction_json,actual_json,evaluation_status) VALUES(?,?,?,?,?,?,?,?)",
                ("fuel_rate", "v1", "refill:2", NOW - 120, NOW - 60,
                 '{"value":1,"features":{"baseline_kg_per_day":2}}', None, "pending"),
            )
        result = read_sqlite_data_audit(self.path, now=NOW)
        self.assertEqual(result["shadow_labels"]["verified"], 1)
        self.assertEqual(result["shadow_labels"]["pending"], 1)
        self.assertEqual(result["training_readiness"]["status"], "manual_review_required")
        self.assertFalse(result["training_readiness"]["training_eligibility"])

    def test_missing_database_is_not_created(self):
        missing = self.path.parent / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            read_sqlite_data_audit(missing, now=NOW)
        self.assertFalse(missing.exists())

    def test_rejects_invalid_window(self):
        with self.assertRaises(ValueError):
            read_sqlite_data_audit(self.path, now=NOW, days=91)


if __name__ == "__main__":
    unittest.main()
