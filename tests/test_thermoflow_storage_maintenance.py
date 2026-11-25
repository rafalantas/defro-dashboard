"""Tests for backup-first, narrowly scoped ThermoFlow cleanup."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_storage_maintenance import cleanup, plan_cleanup


class ThermoFlowStorageMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO dashboard_cache(cache_key,payload_json,generated_at,expires_at) "
                "VALUES('expired','{}',100,150)"
            )
            db.execute(
                "INSERT INTO dashboard_cache(cache_key,payload_json,generated_at,expires_at) "
                "VALUES('live','{}',100,500)"
            )
            db.execute(
                "INSERT INTO collection_runs(collector_name,started_at,completed_at,fetched,"
                "inserted,duplicates,invalid,status,details_json) "
                "VALUES('old',100,101,0,0,0,0,'complete','{}')"
            )
            db.execute(
                "INSERT INTO collection_runs(collector_name,started_at,completed_at,fetched,"
                "inserted,duplicates,invalid,status,details_json) "
                "VALUES('recent',400,401,0,0,0,0,'complete','{}')"
            )
            db.execute(
                "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,"
                "payload_json,payload_sha256) VALUES('thingspeak:test','1:1',100,101,'{}',?)",
                ("a" * 64,),
            )
            db.execute(
                "INSERT INTO fibaro_events(event_id,observed_at,event_type,payload_json,"
                "payload_sha256,collected_at) VALUES(1,100,'event','{}',?,101)",
                ("b" * 64,),
            )

    def tearDown(self):
        self.temp.cleanup()

    def counts(self):
        with sqlite3.connect(self.path) as db:
            return {
                "cache": db.execute("SELECT COUNT(*) FROM dashboard_cache").fetchone()[0],
                "runs": db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0],
                "telemetry": db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0],
                "fibaro": db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0],
            }

    def test_default_is_read_only_dry_run(self):
        before = self.path.stat().st_size
        report = cleanup(self.path, now=200 * 86400 + 1000)
        self.assertEqual(report["mode"], "dry_run")
        self.assertFalse(report["writes_performed"])
        self.assertEqual(report["eligible_total"], 2)
        self.assertEqual(self.path.stat().st_size, before)
        self.assertEqual(self.counts(), {"cache": 2, "runs": 2, "telemetry": 1, "fibaro": 1})

    def test_apply_requires_backup_and_refuses_overwrite(self):
        with self.assertRaises(ValueError):
            cleanup(self.path, now=200 * 86400 + 1000, apply=True)
        backup = self.root / "backup.sqlite3"
        backup.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            cleanup(self.path, now=200 * 86400 + 1000, apply=True, backup_path=backup)

    def test_apply_creates_verified_backup_and_only_deletes_eligible_rows(self):
        backup = self.root / "verified-backup.sqlite3"
        result = cleanup(self.path, now=200 * 86400 + 1000, apply=True, backup_path=backup)
        self.assertEqual(result["mode"], "applied")
        self.assertTrue(backup.is_file())
        self.assertEqual(result["backup_integrity_check"], ["ok"])
        self.assertEqual(result["post_cleanup_integrity_check"], ["ok"])
        self.assertEqual(result["deleted"], {
            "expired_dashboard_cache": 1, "old_collection_runs": 1
        })
        self.assertEqual(self.counts(), {"cache": 1, "runs": 1, "telemetry": 1, "fibaro": 1})
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dashboard_cache").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 2)

    def test_invalid_retention_is_rejected(self):
        with self.assertRaises(ValueError):
            plan_cleanup(self.path, now=200 * 86400 + 1000, run_retention_days=1)


if __name__ == "__main__":
    unittest.main()
