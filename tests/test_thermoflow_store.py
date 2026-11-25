"""Tests for the shared ThermoFlow SQLite store and SQL migration runner."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import MigrationError, SQLiteStore


MIGRATION_SQL = """
CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
"""


class ThermoFlowSQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "thermoflow.sqlite3"
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        (self.migrations / "001_initial.sql").write_text(MIGRATION_SQL, encoding="utf-8")
        self.store = SQLiteStore(self.db_path, migrations_dir=self.migrations)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_is_applied_once_and_is_idempotent(self):
        first = self.store.migrate()
        second = self.store.migrate()
        self.assertEqual(first["applied"], [1])
        self.assertEqual(second["applied"], [])
        self.assertEqual(self.store.schema_version(), 1)
        self.assertEqual(self.store.journal_mode(), "wal")

    def test_migration_and_ledger_row_are_atomic(self):
        (self.migrations / "002_broken.sql").write_text(
            "CREATE TABLE broken (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;\n",
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            self.store.migrate()
        with self.store.connection() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='broken'"
            ).fetchone())
            self.assertEqual(db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 1)

    def test_applied_migration_checksum_is_immutable(self):
        self.store.migrate()
        (self.migrations / "001_initial.sql").write_text(
            MIGRATION_SQL + "\nCREATE INDEX sample_value ON sample(value);\n",
            encoding="utf-8",
        )
        with self.assertRaises(MigrationError):
            self.store.migrate()

    def test_backup_api_copies_committed_data_and_passes_integrity_check(self):
        self.store.migrate()
        with self.store.connection() as db:
            db.execute("INSERT INTO sample(value) VALUES('persisted')")
        backup_path = self.root / "backups" / "copy.sqlite3"
        result = self.store.backup_to(backup_path)
        self.assertTrue(result["ok"])
        copy = SQLiteStore(backup_path, migrations_dir=self.migrations)
        with copy.connection() as db:
            row = db.execute("SELECT value FROM sample").fetchone()
        self.assertEqual(row["value"], "persisted")
        self.assertTrue(copy.integrity_check()["ok"])

    def test_backup_cannot_overwrite_the_source(self):
        with self.assertRaises(ValueError):
            self.store.backup_to(self.db_path)


    def test_initial_schema_contains_all_shared_thermoflow_domains(self):
        self.store = SQLiteStore(
            self.db_path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        expected = {
            "telemetry_samples", "fibaro_events", "fibaro_snapshots",
            "refill_events", "weather_forecasts", "feature_windows", "datasets",
            "model_versions", "model_evaluations", "shadow_predictions",
            "dashboard_cache", "collector_state", "collection_runs",
        }
        with self.store.connection() as db:
            actual = {
                row["name"] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(expected.issubset(actual))

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            SQLiteStore(self.db_path, migrations_dir=self.migrations, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
