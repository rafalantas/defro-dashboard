"""Tests for read-only ThermoFlow SQLite storage diagnostics."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_storage_diagnostics import inspect_storage


class ThermoFlowStorageDiagnosticsTests(unittest.TestCase):
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

    def test_reports_largest_payload_tables_and_database_pages(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO telemetry_samples(source,source_key,observed_at,recorded_at,"
                "payload_json,payload_sha256) VALUES(?,?,?,?,?,?)",
                ("thingspeak:test", "1:1", 100, 101, '{"temperature":21.5}', "a" * 64),
            )
            db.execute(
                "INSERT INTO fibaro_events(event_id,observed_at,event_type,payload_json,"
                "payload_sha256,collected_at) VALUES(?,?,?,?,?,?)",
                (1, 100, "event", '{"id":1}', "b" * 64, 101),
            )

        report = inspect_storage(self.path)
        tables = {item["table"]: item for item in report["tables"]}
        self.assertEqual(report["schema"], "thermoflow-storage-audit/1")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["maintenance_performed"])
        self.assertGreater(report["allocated_bytes"], 0)
        self.assertEqual(tables["telemetry_samples"]["rows"], 1)
        self.assertEqual(tables["telemetry_samples"]["payload_bytes"], len('{"temperature":21.5,"note":"żółć"}'.encode("utf-8")))
        self.assertGreaterEqual(report["total_rows"], 2)

    def test_diagnostic_does_not_change_database_bytes_or_rows(self):
        before_size = self.path.stat().st_size
        with sqlite3.connect(self.path) as db:
            before_rows = db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
        inspect_storage(self.path)
        after_size = self.path.stat().st_size
        with sqlite3.connect(self.path) as db:
            after_rows = db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
        self.assertEqual(after_size, before_size)
        self.assertEqual(after_rows, before_rows)

    def test_missing_database_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            inspect_storage(self.path.parent / "missing.sqlite3")


if __name__ == "__main__":
    unittest.main()
