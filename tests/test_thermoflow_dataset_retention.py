"""Retention of superseded candidate datasets and curation backups."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from thermoflow_analytics_scheduler import run_once
from thermoflow_dataset_retention import ELIGIBLE_DATASETS, retire_superseded_candidates
from thermoflow_fibaro_curate import (
    BACKUP_PREFIX, DATASET_NAME, curate_database, dataset_version_for, prune_curation_backups,
)
from thermoflow_store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database, migrations_dir=ROOT / "db" / "migrations")
        self.store.migrate()

    def add_dataset(self, name, version, created_at, status="candidate", feature_set=None, windows=3):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO datasets(name,version,created_at,manifest_json,content_sha256,status) "
                "VALUES(?,?,?,?,?,?)", (name, version, created_at, "{}", version, status))
            for index in range(windows if feature_set else 0):
                db.execute(
                    "INSERT INTO feature_windows(feature_set,window_start,window_end,generated_at,"
                    "dataset_version,values_json) VALUES(?,?,?,?,?,'{}')",
                    (feature_set, 1000 + index, 1001 + index, created_at, version))

    def counts(self):
        with self.store.connection() as db:
            return (db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM feature_windows").fetchone()[0])

    def test_keeps_only_newest_version_per_dataset_and_removes_its_windows(self):
        for index, version in enumerate(("v3-a", "v3-b", "v3-c")):
            self.add_dataset("fibaro_heating_history_v3", version, 100 + index, feature_set="fibaro_heating_event_v3")
        result = retire_superseded_candidates(self.database)
        self.assertEqual(result["status"], "retired")
        self.assertEqual(result["datasets_retired"], 2)
        self.assertEqual(result["feature_windows_removed"], 6)
        self.assertEqual(result["per_dataset"]["fibaro_heating_history_v3"]["kept"], ["v3-c"])
        self.assertEqual(self.counts(), (1, 3))

    def test_dry_run_changes_nothing_but_reports_the_same_numbers(self):
        for index, version in enumerate(("v3-a", "v3-b")):
            self.add_dataset("fibaro_heating_history_v3", version, 100 + index, feature_set="fibaro_heating_event_v3")
        before = self.counts()
        result = retire_superseded_candidates(self.database, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual((result["datasets_retired"], result["feature_windows_removed"]), (1, 3))
        self.assertEqual(self.counts(), before)

    def test_is_idempotent(self):
        for index, version in enumerate(("v3-a", "v3-b")):
            self.add_dataset("fibaro_heating_history_v3", version, 100 + index, feature_set="fibaro_heating_event_v3")
        retire_superseded_candidates(self.database)
        again = retire_superseded_candidates(self.database)
        self.assertEqual(again["status"], "nothing_to_retire")
        self.assertEqual(self.counts(), (1, 3))

    def test_aggregates_cover_both_hourly_and_daily_windows(self):
        for index, version in enumerate(("v1-old", "v1-new")):
            self.add_dataset("thermoflow_heating_aggregates", version, 100 + index, windows=0)
            with self.store.connection() as db:
                for feature_set in ELIGIBLE_DATASETS["thermoflow_heating_aggregates"]:
                    db.execute(
                        "INSERT INTO feature_windows(feature_set,window_start,window_end,generated_at,"
                        "dataset_version,values_json) VALUES(?,?,?,?,?,'{}')",
                        (feature_set, 1, 2, 1, version))
        result = retire_superseded_candidates(self.database)
        self.assertEqual(result["feature_windows_removed"], 2)
        self.assertEqual(self.counts(), (1, 2))

    def test_never_touches_non_candidates_referenced_or_unlisted_datasets(self):
        name = "fibaro_heating_history_v3"
        self.add_dataset(name, "v3-promoted", 100, status="promoted", feature_set="fibaro_heating_event_v3")
        self.add_dataset(name, "v3-referenced", 101, feature_set="fibaro_heating_event_v3")
        self.add_dataset(name, "v3-plain", 102, feature_set="fibaro_heating_event_v3")
        self.add_dataset(name, "v3-newest", 103, feature_set="fibaro_heating_event_v3")
        self.add_dataset("burner_ml_dataset", "b-old", 50, feature_set="burner_feature")
        self.add_dataset("burner_ml_dataset", "b-new", 60, feature_set="burner_feature")
        with self.store.connection() as db:
            db.execute("INSERT INTO model_evaluations(model_name,model_version,dataset_version,evaluated_at,"
                       "metrics_json,status) VALUES('m','1','v3-referenced',1,'{}','ok')")
        result = retire_superseded_candidates(self.database)
        info = result["per_dataset"][name]
        self.assertEqual(info["retired"], ["v3-plain"])
        self.assertEqual(sorted(info["protected"]), ["v3-promoted", "v3-referenced"])
        with self.store.connection() as db:
            remaining = {row[0] for row in db.execute("SELECT version FROM datasets")}
        self.assertEqual(remaining, {"v3-promoted", "v3-referenced", "v3-newest", "b-old", "b-new"})

    def test_model_version_metadata_reference_protects_a_dataset(self):
        name = "fibaro_heating_history_v3"
        self.add_dataset(name, "v3-used", 100, feature_set="fibaro_heating_event_v3")
        self.add_dataset(name, "v3-newest", 101, feature_set="fibaro_heating_event_v3")
        with self.store.connection() as db:
            db.execute("INSERT INTO model_versions(model_name,version,created_at,metadata_json) "
                       "VALUES('m','1',1,?)", (json.dumps({"dataset_version": "v3-used"}),))
        result = retire_superseded_candidates(self.database)
        self.assertEqual(result["datasets_retired"], 0)
        self.assertEqual(result["per_dataset"][name]["protected"], ["v3-used"])

    def test_rejects_bad_arguments_and_missing_database(self):
        with self.assertRaises(ValueError):
            retire_superseded_candidates(self.database, keep=0)
        with self.assertRaises(FileNotFoundError):
            retire_superseded_candidates(Path(self.tempdir.name) / "missing.sqlite3")


class BackupRetentionTests(unittest.TestCase):
    def test_keeps_newest_backups_and_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for stamp in ("20261126-100000", "20261126-160000", "20261127-040000", "20261127-100000"):
                (directory / f"{BACKUP_PREFIX}{stamp}.sqlite3").write_bytes(b"x" * 10)
            unrelated = directory / "thermoflow-pre-ssd.sqlite3"
            unrelated.write_bytes(b"keep me")
            dry = prune_curation_backups(directory, keep=2, dry_run=True)
            self.assertEqual(len(dry["removed"]), 2)
            self.assertEqual(len(list(directory.glob(f"{BACKUP_PREFIX}*"))), 4)   # dry run deleted nothing
            result = prune_curation_backups(directory, keep=2)
            self.assertEqual(result["bytes_freed"], 20)
            self.assertEqual(result["kept"], [f"{BACKUP_PREFIX}20261127-040000.sqlite3",
                                              f"{BACKUP_PREFIX}20261127-100000.sqlite3"])
            self.assertTrue(unrelated.exists())

    def test_validates_keep_and_tolerates_missing_directory(self):
        with self.assertRaises(ValueError):
            prune_curation_backups("anywhere", keep=0)
        self.assertEqual(prune_curation_backups("does-not-exist", keep=2)["removed"], [])


class SchedulerRegressionTests(unittest.TestCase):
    """The scheduler used to look up a v2 dataset while curation stored v3, so
    every cycle looked "changed" and made a full database backup. These tests
    use the real curation (no mock) so the two can never drift apart silently."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database, migrations_dir=ROOT / "db" / "migrations")
        self.store.migrate()
        snapshot = {"zones": [{"id": 7, "name": "Salon", "properties": {
                        "currentTemperatureHeating": 20.0, "currentSetpointHeating": 21.0, "devices": [70]}}],
                    "heads": [{"id": 70, "properties": {"heatRequired": True}}]}
        with self.store.connection() as db:
            db.execute("INSERT INTO fibaro_snapshots(captured_at,payload_json,payload_sha256) VALUES(1000,?,'s')",
                       (json.dumps(snapshot),))
            for index in range(3):
                event = {"id": index + 1, "timestamp": 2000 + index * 600, "type": "ClimateZoneTemperatureChangedEvent",
                         "data": {"id": 7, "oldValue": 20.0 + index * 0.1, "newValue": 20.1 + index * 0.1}}
                db.execute("INSERT INTO fibaro_events(event_id,observed_at,event_type,payload_json,payload_sha256,"
                           "collected_at) VALUES(?,?,?,?,?,?)",
                           (index + 1, event["timestamp"], event["type"], json.dumps(event), f"e{index}", 3000))

    def backups(self):
        return list((self.database.parent / "backups").glob(f"{BACKUP_PREFIX}*.sqlite3"))

    def test_unchanged_content_does_not_back_up_again(self):
        first = run_once(str(self.database), now=5000)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["curation"]["status"], "applied")
        self.assertEqual(len(self.backups()), 1)
        second = run_once(str(self.database), now=5600)
        self.assertEqual(second["curation"]["status"], "unchanged")
        self.assertEqual(len(self.backups()), 1)          # was one backup per cycle before the fix

    def test_scheduler_and_curation_agree_on_the_stored_version(self):
        run_once(str(self.database), now=5000)
        with self.store.connection() as db:
            row = db.execute("SELECT version,content_sha256 FROM datasets WHERE name=?", (DATASET_NAME,)).fetchone()
        self.assertEqual(row["version"], dataset_version_for(row["content_sha256"]))

    def test_old_versions_are_retired_after_content_changes(self):
        run_once(str(self.database), now=5000)
        with self.store.connection() as db:
            event = {"id": 99, "timestamp": 9000, "type": "ClimateZoneTemperatureChangedEvent",
                     "data": {"id": 7, "oldValue": 20.3, "newValue": 19.9}}
            db.execute("INSERT INTO fibaro_events(event_id,observed_at,event_type,payload_json,payload_sha256,"
                       "collected_at) VALUES(99,9000,?,?,'e99',9001)", (event["type"], json.dumps(event)))
        second = run_once(str(self.database), now=9500)
        self.assertEqual(second["curation"]["status"], "applied")
        self.assertGreaterEqual(second["retention"]["datasets_retired"], 1)
        with self.store.connection() as db:
            versions = [r[0] for r in db.execute("SELECT version FROM datasets WHERE name=?", (DATASET_NAME,))]
        self.assertEqual(len(versions), 1)


if __name__ == "__main__":
    unittest.main()
