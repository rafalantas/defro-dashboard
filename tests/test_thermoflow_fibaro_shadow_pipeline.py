"""Tests for the persisted Fibaro quality-to-shadow pipeline."""
import unittest
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from thermoflow_fibaro_shadow_pipeline import run_fibaro_shadow_pipeline
from thermoflow_fibaro_shadow_evaluation import evaluate_fibaro_shadow


class FakeCollector:
    def __init__(self, events=None, snapshot=None):
        self.events = events or []
        self.snapshot = snapshot

    def read_latest_snapshot(self):
        return self.snapshot

    def read_events(self, *, start=None, end=None, limit=10000):
        return [event for event in self.events
                if (start is None or event["timestamp"] >= start)
                and (end is None or event["timestamp"] <= end)][:limit]


def evidence(days=32):
    zone = ZoneInfo("Europe/Warsaw")
    first = datetime(2026, 1, 1, tzinfo=zone).date()
    snapshot = {
        "zones": [{"id": 7, "name": "Salon", "properties": {"devices": [2162]}}],
        "heads": [{"id": 2162, "properties": {"heatRequired": True}}],
    }
    events = []
    event_id = 1
    for index in range(days):
        day = first + timedelta(days=index)
        stamp = int(datetime.combine(day, time(10), tzinfo=zone).timestamp())
        heat_new = bool(index % 2 == 0)
        window_new = bool(index % 3 == 0)
        events.extend([
            {"id": event_id, "timestamp": stamp, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "heatRequired",
                      "oldValue": not heat_new, "newValue": heat_new}},
            {"id": event_id + 1, "timestamp": stamp + 10, "type": "DevicePropertyUpdatedEvent",
             "data": {"id": 2162, "property": "windowOpened",
                      "oldValue": not window_new, "newValue": window_new}},
            {"id": event_id + 2, "timestamp": stamp + 20, "type": "ClimateZoneTemperatureChangedEvent",
             "data": {"id": 7, "oldValue": 19.0 + (index % 4) / 10,
                      "newValue": 19.2 + (index % 5) / 10, "unit": "C"}},
        ])
        event_id += 3
    start = int(datetime.combine(first, time.min, tzinfo=zone).timestamp())
    end = int(datetime.combine(first + timedelta(days=days), time.min, tzinfo=zone).timestamp()) - 1
    return FakeCollector(events, {
        "captured_at": end,
        "payload": snapshot,
    }), start, end


class FibaroShadowPipelineTests(unittest.TestCase):
    def test_missing_persisted_snapshot_fails_closed(self):
        result = run_fibaro_shadow_pipeline(FakeCollector(), start=100, end=200)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["reason"], "persisted_fibaro_snapshot_required")
        self.assertFalse(result["safety"]["hc3_requests"])

    def test_quality_gate_and_daily_features_use_persisted_evidence(self):
        collector, start, end = evidence()
        result = run_fibaro_shadow_pipeline(
            collector, start=start, end=end, minimum_events=50, minimum_observed_days=7
        )
        self.assertEqual(result["quality"]["status"], "ready")
        self.assertEqual(result["evidence"]["events"], 96)
        self.assertEqual(result["evidence"]["observed_days_with_transitions"], 32)
        self.assertEqual(result["readiness"]["quality_gate"], "ready")
        self.assertTrue(result["readiness"]["quality_gate_eligible"])
        self.assertEqual(result["readiness"]["observed_days"], 32)
        self.assertEqual(result["readiness"]["longest_consecutive_observed_days"], 32)
        self.assertEqual(result["readiness"]["eligible_samples"], 31)
        self.assertEqual(result["readiness"]["required_samples"], 25)
        self.assertEqual(result["readiness"]["required_training_samples"], 20)
        self.assertEqual(result["readiness"]["required_holdout_samples"], 5)
        self.assertEqual(result["readiness"]["promotion"], "disabled")
        self.assertEqual(result["mode"], "offline_shadow_only")
        self.assertFalse(result["safety"]["hc3_requests"])
        self.assertFalse(result["safety"]["automatic_promotion"])
        self.assertEqual(result["safety"]["return_protection"], "55°C ±1°C")
        self.assertIn(result["status"], ("evaluated", "collecting"))

    def test_unknown_events_do_not_satisfy_quality_volume_or_day_gates(self):
        collector, start, end = evidence(days=10)
        collector.events = [
            {"id": 9000 + index, "timestamp": start + index * 86400,
             "type": "UnrecognizedHC3Event", "data": {"value": index}}
            for index in range(10)
        ]
        result = run_fibaro_shadow_pipeline(
            collector, start=start, end=end, minimum_events=1, minimum_observed_days=1
        )
        self.assertEqual(result["quality"]["status"], "collecting")
        self.assertEqual(result["quality"]["counts"]["recognized_events"], 0)
        self.assertEqual(result["quality"]["counts"]["unknown_event_types"], 10)
        self.assertEqual(result["quality"]["counts"]["observed_utc_days"], 0)

    def test_missing_days_are_not_synthesized(self):
        collector, start, end = evidence()
        collector.events = [
            event for event in collector.events
            if not (datetime.fromtimestamp(event["timestamp"], timezone.utc).day == 6)
        ]
        result = run_fibaro_shadow_pipeline(
            collector, start=start, end=end, minimum_events=1, minimum_observed_days=1
        )
        dates = [row["date"] for row in result["daily_features"]]
        self.assertLess(len(dates), 32)
        self.assertEqual(len(dates), len(set(dates)))

    def test_negative_linear_predictions_are_floored_and_reported(self):
        model = {
            "model_schema_version": "1.0.0",
            "mode": "read_only_shadow",
            "feature_keys": ["x"],
            "target_key": "y",
            "feature_means": [0.0],
            "feature_scales": [1.0],
            "intercept": -2.0,
            "coefficients": [0.0],
        }
        training = [
            {"timestamp": 20260101 + index, "features": {"x": 0.0}, "target": 1.0}
            for index in range(20)
        ]
        # Use valid chronological YYYYMMDD values spanning actual calendar days.
        first = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        training = [
            {"timestamp": int((first + timedelta(days=index)).strftime("%Y%m%d")),
             "features": {"x": 0.0}, "target": 1.0}
            for index in range(20)
        ]
        holdout = [
            {"timestamp": int((first + timedelta(days=20 + index)).strftime("%Y%m%d")),
             "features": {"x": 0.0}, "target": 0.0}
            for index in range(5)
        ]
        result = evaluate_fibaro_shadow({
            "status": "evaluated",
            "dataset": {"training": training, "holdout": holdout},
            "model": model,
            "evaluation": {"holdout_is_unseen": True},
        })
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["summary"]["negative_predictions_clipped"], 5)
        self.assertEqual(result["summary"]["candidate_mae"], 0.0)
        self.assertEqual(result["prediction_audit"][0]["raw_prediction"], -2.0)
        self.assertEqual(result["prediction_audit"][0]["scored_prediction"], 0.0)

    def test_invalid_prediction_returns_unavailable_instead_of_crashing(self):
        model = {
            "model_schema_version": "1.0.0",
            "mode": "read_only_shadow",
            "feature_keys": ["x"],
            "target_key": "y",
            "feature_means": [0.0],
            "feature_scales": [0.0],
            "intercept": 0.0,
            "coefficients": [0.0],
        }
        first = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        training = [
            {"timestamp": int((first + timedelta(days=i)).strftime("%Y%m%d")),
             "features": {"x": 0.0}, "target": 1.0}
            for i in range(20)
        ]
        holdout = [
            {"timestamp": int((first + timedelta(days=20+i)).strftime("%Y%m%d")),
             "features": {"x": 0.0}, "target": 1.0}
            for i in range(5)
        ]
        result = evaluate_fibaro_shadow({
            "status": "evaluated", "dataset": {"training": training, "holdout": holdout},
            "model": model, "evaluation": {"holdout_is_unseen": True},
        })
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "invalid_holdout_prediction")

    def test_rejects_overlong_window(self):
        with self.assertRaises(ValueError):
            run_fibaro_shadow_pipeline(FakeCollector(), start=1, end=1 + 91 * 86400)


if __name__ == "__main__":
    unittest.main()
