from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from thermoflow_fibaro_curate import dataset_version_for
from thermoflow_store import SQLiteStore
from thermoflow_analytics_scheduler import (
    DATASET_NAME,
    run_once,
    run_scheduler,
    start_background_scheduler,
)


class AnalyticsSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.database)
        self.store.migrate()

    def tearDown(self):
        self.tempdir.cleanup()

    def _add_source_evidence(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fibaro_events(event_id,observed_at,event_type,"
                "payload_json,payload_sha256,collected_at) "
                "VALUES(1,1000,'ClimateZoneTemperatureChangedEvent','{}','event',1001)"
            )
            db.execute(
                "INSERT INTO fibaro_snapshots(captured_at,payload_json,payload_sha256) "
                "VALUES(1100,?,?)",
                (json.dumps({"zones": [{"id": 1}], "heads": [{"id": 2}]}), "snapshot"),
            )

    def test_empty_source_returns_collecting_without_writes(self):
        result = run_once(str(self.database), now=2000)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "no_fibaro_events")
        self.assertFalse(result["writes"])

    def test_missing_snapshot_blocks_curation(self):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO fibaro_events(event_id,observed_at,event_type,"
                "payload_json,payload_sha256,collected_at) "
                "VALUES(1,1000,'ClimateZoneTemperatureChangedEvent','{}','event',1001)"
            )
        result = run_once(str(self.database), now=2000)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "no_fibaro_snapshot_for_identity_validation")

    @patch("thermoflow_analytics_scheduler.curate_database")
    def test_no_validated_features_waits_without_creating_backup(self, curate):
        self._add_source_evidence()
        curate.return_value = {
            "status": "dry_run",
            "content_sha256": "c" * 64,
            "curated_features": 0,
        }

        result = run_once(str(self.database), now=2000)

        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "no_validated_heating_transitions")
        curate.assert_called_once_with(str(self.database), apply=False)

    @patch("thermoflow_analytics_scheduler.run_fibaro_shadow_pipeline")
    @patch("thermoflow_analytics_scheduler.read_heating_analytics")
    @patch("thermoflow_analytics_scheduler.curate_database")
    def test_changed_dataset_is_backed_up_and_derived_outputs_are_persisted(
        self, curate, analytics, shadow
    ):
        self._add_source_evidence()
        digest = "a" * 64
        curate.side_effect = [
            {"status": "dry_run", "content_sha256": digest},
            {"status": "applied", "dataset_version": dataset_version_for(digest)},
        ]
        analytics.return_value = {
            "status": "available",
            "persistence": {"status": "persisted", "writes": 2},
            "source_counts": {"fibaro_feature_windows": 1, "emodul_samples": 1},
        }
        shadow.return_value = {
            "status": "collecting",
            "reason": "insufficient_training_or_holdout_samples",
            "evidence": {"events": 1},
            "quality": {"status": "collecting"},
        }

        result = run_once(str(self.database), now=2000)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(curate.call_count, 2)
        self.assertEqual(curate.call_args_list[0].kwargs, {"apply": False})
        self.assertEqual(curate.call_args_list[1].kwargs, {"apply": True})
        analytics.assert_called_once()
        self.assertTrue(analytics.call_args.kwargs["persist"])
        shadow.assert_called_once()
        self.assertTrue(shadow.call_args.kwargs["persist"])
        self.assertEqual(result["fibaro_shadow"]["status"], "collecting")
        self.assertFalse(result["safety"]["hc3_requests"])
        self.assertFalse(result["safety"]["raw_fibaro_events_deleted"])

    @patch("thermoflow_analytics_scheduler.run_fibaro_shadow_pipeline")
    @patch("thermoflow_analytics_scheduler.read_heating_analytics")
    @patch("thermoflow_analytics_scheduler.curate_database")
    def test_unchanged_dataset_skips_backup_and_reuses_candidate(
        self, curate, analytics, shadow
    ):
        self._add_source_evidence()
        digest = "b" * 64
        version = dataset_version_for(digest)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO datasets(name,version,created_at,manifest_json,"
                "content_sha256,status) VALUES(?,?,?,?,?,?)",
                (DATASET_NAME, version, 1200, "{}", digest, "candidate"),
            )
        curate.return_value = {"status": "dry_run", "content_sha256": digest}
        analytics.return_value = {"status": "collecting", "persistence": {"status": "skipped"}}
        shadow.return_value = {"status": "collecting"}

        result = run_once(str(self.database), now=2000)

        self.assertEqual(result["status"], "completed")
        curate.assert_called_once_with(str(self.database), apply=False)
        self.assertEqual(result["curation"]["status"], "unchanged")
        self.assertFalse(result["safety"]["raw_fibaro_events_deleted"])

    def test_scheduler_records_errors_and_continues(self):
        calls = {"count": 0}

        def cycle():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("private detail must not be logged")
            return {"status": "completed"}

        results = run_scheduler(
            cycle, interval_seconds=3600, max_cycles=2, wait=lambda _seconds: None
        )
        self.assertEqual([item["status"] for item in results], ["error", "completed"])
        self.assertEqual(results[0]["error_type"], "RuntimeError")
        self.assertNotIn("private detail", json.dumps(results))

    def test_background_scheduler_starts_daemon_after_initial_delay(self):
        created = {}
        waits = []

        class FakeThread:
            def __init__(self, *, target, name, daemon):
                created.update(target=target, name=name, daemon=daemon)
                self.started = False

            def start(self):
                self.started = True

            def run(self):
                created["target"]()

        with patch("thermoflow_analytics_scheduler.run_scheduler") as scheduler:
            thread = start_background_scheduler(
                "/tmp/shared.sqlite3",
                enabled=True,
                interval_seconds=21600,
                initial_delay_seconds=120,
                thread_factory=FakeThread,
                wait=waits.append,
            )
            self.assertIs(thread.started, True)
            self.assertEqual(created["name"], "thermoflow-analytics")
            self.assertIs(created["daemon"], True)
            thread.run()

        self.assertEqual(waits, [120])
        scheduler.assert_called_once()
        self.assertEqual(scheduler.call_args.kwargs["interval_seconds"], 21600)

    def test_background_scheduler_can_be_disabled(self):
        factory = Mock()
        result = start_background_scheduler(
            enabled=False,
            thread_factory=factory,
        )
        self.assertIsNone(result)
        factory.assert_not_called()

    def test_background_scheduler_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            start_background_scheduler(enabled=True, interval_seconds=300)
        with self.assertRaises(ValueError):
            start_background_scheduler(
                enabled=True, interval_seconds=21600, initial_delay_seconds=3601
            )

    def test_scheduler_rejects_too_frequent_cycles(self):
        with self.assertRaises(ValueError):
            run_scheduler(lambda: {}, interval_seconds=300, max_cycles=1)


if __name__ == "__main__":
    unittest.main()
