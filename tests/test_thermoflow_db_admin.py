"""Tests for the explicit SQLite operator CLI primitives."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_db_admin import create_backup, inspect_database


class ThermoFlowSQLiteAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.db_path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()

    def tearDown(self):
        self.temp.cleanup()

    def test_status_is_read_only_and_reports_migrations(self):
        before = self.db_path.stat().st_mtime_ns
        result = inspect_database(self.db_path)
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["journal_mode"], "wal")
        self.assertGreaterEqual(result["schema_version"], 2)
        self.assertEqual(self.db_path.stat().st_mtime_ns, before)

    def test_backup_is_verified_and_does_not_overwrite(self):
        target = self.root / "backup.sqlite3"
        result = create_backup(self.db_path, target)
        self.assertEqual(result["status"], "backup_created")
        self.assertEqual(result["integrity_check"], ["ok"])
        self.assertTrue(target.is_file())
        with self.assertRaises(FileExistsError):
            create_backup(self.db_path, target)

    def test_backup_cannot_target_source(self):
        with self.assertRaises(ValueError):
            create_backup(self.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main()
