import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from backup import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite3"
        self.backups = self.root / "backups"
        db = sqlite3.connect(self.source)
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('wal-aware')")
        db.commit()
        db.close()
        self.source_patch = patch.object(backup, "SOURCE_DB", self.source)
        self.backup_patch = patch.object(backup, "BACKUP_DIR", self.backups)
        self.source_patch.start()
        self.backup_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.backup_patch.stop()
        self.temp.cleanup()

    def test_snapshot_is_valid_and_contains_data(self):
        snapshot = backup.make_snapshot()
        self.assertTrue(snapshot.is_file())
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        db = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.execute("SELECT value FROM sample").fetchone()[0], "wal-aware")
        db.close()

    def test_missing_source_fails_without_publishing_temp_file(self):
        with patch.object(backup, "SOURCE_DB", self.root / "missing.sqlite3"):
            with self.assertRaises(FileNotFoundError):
                backup.make_snapshot()
        self.assertEqual(list(self.backups.glob("*.tmp")), [])

    def test_integrity_failure_does_not_publish_snapshot(self):
        with patch.object(backup, "sqlite3") as sqlite_mock:
            sqlite_mock.connect.side_effect = [Mock(), Mock()]
            sqlite_mock.connect.return_value.execute.return_value.fetchone.return_value = ("corrupt",)
            with self.assertRaises(RuntimeError):
                backup.make_snapshot()
        self.assertEqual(list(self.backups.glob("thermoflow-hourly-*.sqlite3")), [])

    def test_repository_probe_initializes_only_for_exit_10(self):
        with patch.object(backup, "restic", side_effect=[
            Mock(returncode=10), Mock(returncode=0)
        ]) as runner:
            backup.ensure_repository()
        self.assertEqual(runner.call_args_list[0].args[0], ("snapshots", "--json"))
        self.assertEqual(runner.call_args_list[1].args[0], ("init",))

    def test_repository_access_error_does_not_initialize(self):
        with patch.object(backup, "restic", return_value=Mock(returncode=12)) as runner:
            with self.assertRaises(RuntimeError):
                backup.ensure_repository()
        runner.assert_called_once()

    def test_schedule_boundaries(self):
        current = datetime(2026, 11, 28, 10, 4, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
        self.assertEqual(backup.next_hourly(current), current.replace(minute=5, second=0, microsecond=0))
        current = datetime(2026, 11, 28, 10, 5, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        self.assertEqual(backup.next_hourly(current).hour, 11)
        self.assertEqual(backup.next_offsite(current).hour, backup.OFFSITE_HOUR)

    def test_config_rejects_invalid_retention(self):
        with patch.object(backup, "LOCAL_RETENTION_HOURS", 0):
            with self.assertRaises(ValueError):
                backup.validate_config()


if __name__ == "__main__":
    unittest.main()
