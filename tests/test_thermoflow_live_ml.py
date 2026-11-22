import json
import http.client
import threading
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import api
from thermoflow_shadow_runtime import ShadowRuntime
from thermoflow_unified_runtime import UnifiedRuntime

from thermoflow_fuel_live import (
    ARTIFACT_SCHEMA as FUEL_SCHEMA,
    MAX_VERIFIED_PERIOD_AGE_SECONDS,
    build_live_artifact as build_fuel_artifact,
    load_live_artifact as load_fuel_artifact,
    predict_fuel_rate,
)
from thermoflow_live_ml import LiveMLRuntime

NOW = 1_810_000_000


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def fuel_inputs():
    holdout = []
    for index in range(5):
        start = NOW - (10 - index) * 86400
        holdout.append({
            "timestamp": float(start),
            "target": 4.0 + index * 0.1,
            "features": {"baseline_kg_per_day": 4.2, "previous_kg_per_day": 3.9},
        })
    last = holdout[-1]
    start = int(last["timestamp"])
    end = start + 86400
    snapshot = {
        "schema": "thermoflow-verified-fuel-periods/1",
        "baseline_mode": "as_issued",
        "label_mode": "verified_equal_fill_level",
        "as_of": iso(end),
        "periods": [{
            "start": iso(start), "end": iso(end),
            "refilled_kg": last["target"],
            "mass_reference": "verified_equal_fill_level",
            "baseline_issued_at": iso(start - 3600),
        }],
    }
    model = {
        "model_schema_version": "1.0.0",
        "dataset_schema_version": "1.0.0",
        "algorithm": "ordinary_least_squares",
        "feature_keys": ["baseline_kg_per_day", "previous_kg_per_day"],
        "target_key": "observed_kg_per_day",
        "feature_means": [4.0, 4.0],
        "feature_scales": [1.0, 1.0],
        "intercept": 4.0,
        "coefficients": [0.2, 0.1],
        "training_samples": 20,
        "holdout_samples": 5,
        "mode": "read_only_shadow",
    }
    benchmark = {
        "schema": "thermoflow-fuel-shadow/1",
        "status": "evaluated",
        "model": model,
        "dataset": {"eligible_samples": 25, "holdout": holdout},
        "evaluation": {
            "candidate": {"samples": 5, "mae_kg_per_day": 0.1,
                          "rmse_kg_per_day": 0.2, "bias_kg_per_day": 0.0},
            "as_issued_baseline": {"samples": 5, "mae_kg_per_day": 0.8,
                                   "rmse_kg_per_day": 1.0, "bias_kg_per_day": 0.1},
            "previous_period": {"samples": 5, "mae_kg_per_day": 0.5,
                                "rmse_kg_per_day": 0.7, "bias_kg_per_day": -0.1},
        },
    }
    return snapshot, benchmark


def burner_artifact():
    from thermoflow_burner_live import build_live_artifact
    model = {
        "model_schema_version": "1.0.0",
        "dataset_schema_version": "1.0.0",
        "algorithm": "ordinary_least_squares",
        "feature_keys": ["previous_duration_minutes", "idle_minutes"],
        "target_key": "duration_minutes",
        "feature_means": [2.0, 1.0],
        "feature_scales": [1.0, 1.0],
        "intercept": 12.0,
        "coefficients": [0.0, 0.0],
        "training_samples": 20,
        "holdout_samples": 5,
        "mode": "read_only_shadow",
    }
    return build_live_artifact({
        "schema": "thermoflow-burner-shadow/1",
        "status": "evaluated",
        "source_snapshot_sha256": "b" * 64,
        "model": model,
        "evaluation": {
            "holdout_samples": 5,
            "candidate": {"samples": 5, "mae_minutes": 0.2,
                          "rmse_minutes": 0.3, "bias_minutes": 0.0,
                          "beats_both_baselines_on_mae": True},
            "baselines": {
                "training_median": {"samples": 5, "mae_minutes": 1.0},
                "previous_cycle": {"samples": 5, "mae_minutes": 0.8},
            },
        },
    }, created_at=NOW)


class FuelLiveArtifactTests(unittest.TestCase):
    def test_requires_verified_as_issued_data_and_beats_both_baselines(self):
        snapshot, benchmark = fuel_inputs()
        artifact = build_fuel_artifact(benchmark, snapshot, created_at=NOW)
        self.assertEqual(artifact["schema"], FUEL_SCHEMA)
        self.assertEqual(artifact["latest_verified_rate_kg_per_day"], 4.4)
        snapshot["label_mode"] = "assumed_equal_fill_level"
        with self.assertRaisesRegex(ValueError, "verified labels"):
            build_fuel_artifact(benchmark, snapshot, created_at=NOW)
        snapshot, benchmark = fuel_inputs()
        benchmark["evaluation"]["candidate"]["mae_kg_per_day"] = 0.6
        with self.assertRaisesRegex(ValueError, "both"):
            build_fuel_artifact(benchmark, snapshot, created_at=NOW)

    def test_load_checks_verified_period_freshness_and_predicts_kg_day(self):
        snapshot, benchmark = fuel_inputs()
        artifact = build_fuel_artifact(benchmark, snapshot, created_at=NOW)
        loaded = load_fuel_artifact(json.dumps(artifact).encode(), now=NOW)
        prediction = predict_fuel_rate(loaded, baseline_kg_per_day=4.5)
        self.assertEqual(prediction["unit"], "kg/d")
        self.assertGreater(prediction["value"], 0)
        with self.assertRaisesRegex(ValueError, "stale"):
            load_fuel_artifact(artifact, now=NOW + MAX_VERIFIED_PERIOD_AGE_SECONDS + 1)

    def test_live_runtime_predicts_burner_and_fuel_without_control_effects(self):
        snapshot, benchmark = fuel_inputs()
        fuel = build_fuel_artifact(benchmark, snapshot, created_at=NOW)
        burner = burner_artifact()
        with tempfile.TemporaryDirectory() as temp:
            burner_path = Path(temp) / "burner.json"
            fuel_path = Path(temp) / "fuel.json"
            burner_path.write_text(json.dumps(burner), encoding="utf-8")
            fuel_path.write_text(json.dumps(fuel), encoding="utf-8")
            runtime = LiveMLRuntime(burner_path=str(burner_path), fuel_path=str(fuel_path))
            runtime.refresh(now=NOW)

            def observe(offset, fire):
                runtime.observe({
                    "connected": True,
                    "fetched_at": iso(NOW - 300 + offset),
                    "data": {"fire": fire},
                })

            observe(0, 0)
            observe(30, 1)
            observe(60, 1)
            observe(90, 0)
            observe(120, 1)
            summary = {
                "configured": True,
                "fetched_at": iso(NOW - 180),
                "planning": {"today": {"average_kg_day": 4.5}},
            }
            result = runtime.read(NOW - 150, summary)
            self.assertEqual(result["burner_ml"]["status"], "predicted")
            self.assertEqual(result["burner_ml"]["prediction"], 12.0)
            self.assertEqual(result["fuel_ml"]["status"], "predicted")
            self.assertEqual(result["fuel_ml"]["unit"], "kg/d")
            self.assertEqual(result["fuel_ml"]["baseline_kg_per_day"], 4.5)
            self.assertEqual(result["burner_ml"]["mode"], "live_advisory")
            self.assertEqual(result["fuel_ml"]["mode"], "live_advisory")

    def test_missing_or_stale_telemetry_never_produces_a_burner_prediction(self):
        runtime = LiveMLRuntime()
        runtime.observe({"connected": False})
        self.assertEqual(runtime.read(NOW)["burner_ml"]["status"], "collecting")
        self.assertEqual(runtime.read(NOW)["fuel_ml"]["status"], "collecting")


class LiveMLApiTests(unittest.TestCase):
    def test_unified_intelligence_exposes_separate_live_predictions(self):
        class FakeLiveRuntime:
            fuel_path = "/configured/fuel-model.json"

            def read(self, _now, _fuel_summary):
                return {
                    "burner_ml": {"status": "predicted", "prediction": 18.5, "unit": "minutes"},
                    "fuel_ml": {"status": "collecting", "reason": "missing_artifact"},
                }

        with ThreadingHTTPServer(("127.0.0.1", 0), api.Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                weather_summary = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "model": {"periods_used": 30},
                    "daily_model": {"status": "aktywny", "days": 30,
                                    "terms": ["hdd", "solar"],
                                    "rmse_loo_kg_per_day": 0.8},
                    "forecast": [
                        {"day": "2026-11-23", "t_mean": 2.0, "solar_mj": 3.0,
                         "wind_max_kmh": 20.0, "kg": 14.5},
                    ],
                }
                with (patch.object(api, "_intelligence_runtime", UnifiedRuntime()),
                      patch.object(api, "_shadow_runtime", ShadowRuntime()),
                      patch.object(api, "_live_ml_runtime", FakeLiveRuntime()),
                      patch.object(api, "_fuel_summary_for_intelligence", return_value=weather_summary)):
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        client.request("GET", "/api/thermoflow/v1/intelligence")
                        response = client.getresponse()
                        payload = json.loads(response.read())
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.getheader("Cache-Control"), "no-store")
                        self.assertEqual(payload["live_predictions"]["burner_ml"]["prediction"], 18.5)
                        self.assertEqual(payload["live_predictions"]["fuel_ml"]["status"], "collecting")
                        self.assertEqual(payload["weather_forecast"]["status"], "ready")
                        self.assertEqual(payload["weather_forecast"]["days"][0]["kg"], 14.5)
                    finally:
                        client.close()
            finally:
                server.shutdown()
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
