import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import api
import thermoflow_unified_runtime
from test_thermoflow_shadow_runtime import payload
from test_thermoflow_weather_impact import fixture
from thermoflow_intelligence_bundle import build_bundle
from thermoflow_observed_cycles import FIRE, INPUT_SCHEMA
from thermoflow_shadow_runtime import PATH as SHADOW_PATH, ShadowRuntime
from thermoflow_unified_runtime import MAX_BUNDLE_AGE, MAX_BYTES, PATH, SCHEMA, UnifiedRuntime

NOW = 1_810_000_000


def raw_fire(count):
    values = []
    for i in range(count):
        values.extend([0] * (5 + 3 * i % 11) + [1] * (4 + i * i % 7))
    values += [0, 0]
    return {"schema": INPUT_SCHEMA, "sampling": "raw", "points": {
        FIRE: [[1_800_000_000 + i * 30, value] for i, value in enumerate(values)]}}


class UnifiedRuntimeTests(unittest.TestCase):
    def test_all_benchmarks_use_existing_evaluators_without_exposing_source_data(self):
        inputs = {"burner_ml": raw_fire(35), "fuel_ml": fixture(), "weather_impact": fixture()}
        bundle = build_bundle(inputs, now=NOW)
        self.assertEqual({item["status"] for item in bundle["modules"].values()}, {"evaluated"})
        self.assertEqual(bundle["modules"]["weather_impact"]["holdout"]["with_weather"]["samples"], 5)
        encoded = json.dumps(bundle)
        for forbidden in ("coefficients", "feature_means", "baseline_issued_at", "refilled_kg", "points", "target"):
            self.assertNotIn(forbidden, encoded)

    def test_insufficient_and_unverified_inputs_never_become_predictions(self):
        bundle = build_bundle({"burner_ml": raw_fire(4)}, now=NOW)
        burner = bundle["modules"]["burner_ml"]
        self.assertEqual(burner["status"], "collecting")
        self.assertIsNone(burner["holdout"])
        for key, value in (("label_mode", "assumed_equal_fill_level"), ("baseline_mode", "causal_replay_median")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "verified refills"):
                build_bundle({"fuel_ml": {**fixture(), key: value}}, now=NOW)
        with self.assertRaisesRegex(ValueError, "future"):
            build_bundle({"burner_ml": raw_fire(35)}, now=1_700_000_000)
        with self.assertRaisesRegex(ValueError, "future"):
            build_bundle({"fuel_ml": fixture()}, now=1_700_000_000)

    def test_future_and_malformed_bundles_do_not_publish_results(self):
        bundle = build_bundle({"fuel_ml": fixture()}, now=NOW + 1)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bundle.json"
            runtime = UnifiedRuntime(path)
            for content in (json.dumps(bundle), '{"schema":', "[]"):
                with self.subTest(content=content[:40]):
                    path.write_text(content, encoding="utf-8")
                    runtime.refresh(now=NOW)
                    entry = runtime.read(NOW, {})["modules"]["fuel_ml"]
                    self.assertEqual((entry["status"], entry["reason"]), ("unavailable", "invalid_bundle"))
                    self.assertIsNone(entry["result"])
                    self.assertIsNone(entry["prediction"])

    def test_bundle_fail_closed_per_module_then_expires_or_is_removed(self):
        bundle = build_bundle({"burner_ml": raw_fire(35), "fuel_ml": fixture()}, now=NOW)
        bundle["modules"]["fuel_ml"]["holdout"]["candidate"]["mae"] = float("nan")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bundle.json"
            runtime = UnifiedRuntime(path)
            self.assertEqual(runtime.read(NOW, {})["modules"]["fuel_ml"]["reason"], "not_loaded")
            path.write_text(json.dumps(bundle), encoding="utf-8")
            runtime.refresh(now=NOW)
            modules = runtime.read(NOW, {})["modules"]
            self.assertEqual(modules["burner_ml"]["status"], "evaluated")
            self.assertIsNone(modules["burner_ml"]["prediction"])
            self.assertEqual(modules["fuel_ml"]["reason"], "invalid_benchmark_report")
            self.assertEqual(modules["weather_impact"]["reason"], "no_benchmark_report")
            self.assertIsNone(runtime.read(NOW + MAX_BUNDLE_AGE + 1, {})["modules"]["burner_ml"]["result"])
            path.write_bytes(b" " * (MAX_BYTES + 1))
            runtime.refresh(now=NOW)
            self.assertEqual(runtime.read(NOW, {})["modules"]["burner_ml"]["reason"], "invalid_bundle")
            path.unlink()
            runtime.refresh(now=NOW)
            self.assertEqual(runtime.read(NOW, {})["modules"]["burner_ml"]["reason"], "missing_bundle")
            os.mkfifo(path)
            runtime.refresh(now=NOW)
            self.assertEqual(runtime.read(NOW, {})["modules"]["burner_ml"]["reason"], "invalid_bundle")

    def test_readers_use_previous_generation_while_refresh_blocks_in_io(self):
        entered, release = threading.Event(), threading.Event()
        runtime = UnifiedRuntime("unused-path")
        burner = build_bundle({"burner_ml": raw_fire(35)}, now=NOW)["modules"]
        fuel = build_bundle({"fuel_ml": fixture()}, now=NOW)["modules"]
        with patch.object(thermoflow_unified_runtime, "_read_bundle", return_value=(NOW - 1, burner)):
            runtime.refresh(now=NOW)

        def slow_read(_):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("refresh was never released")
            return NOW, fuel

        with (patch.object(thermoflow_unified_runtime, "_read_bundle", side_effect=slow_read),
              ThreadPoolExecutor(max_workers=5) as pool):
            refresh = pool.submit(runtime.refresh, now=NOW)
            try:
                self.assertTrue(entered.wait(timeout=2))
                readers = [pool.submit(runtime.read, NOW, {}) for _ in range(16)]
                for reader in readers:
                    snapshot = reader.result(timeout=2)["modules"]
                    self.assertEqual(snapshot["burner_ml"]["evaluated_at"], NOW - 1)
                    self.assertEqual(snapshot["fuel_ml"]["reason"], "no_benchmark_report")
            finally:
                release.set()
            refresh.result(timeout=2)
        snapshot = runtime.read(NOW, {})["modules"]
        self.assertEqual(snapshot["fuel_ml"]["evaluated_at"], NOW)
        self.assertEqual(snapshot["burner_ml"]["reason"], "no_benchmark_report")

    def test_live_shadow_results_and_evidence_expire_together(self):
        shadow, runtime = ShadowRuntime(), UnifiedRuntime()
        for i in range(13):
            shadow.observe(payload(NOW - 360 + 30 * i, temperature=56 if i == 12 else 50))
        original = shadow.read(NOW)
        response = runtime.read(NOW, original)
        self.assertEqual(response["modules"]["health_score"]["result"]["score"], 50)
        result = response["modules"]["diagnostic_ai"]["result"]
        self.assertEqual(result["items"][0]["evidence_ref"], "/screens/return")
        self.assertIn("return", response["screens"])
        self.assertIn("anomaly_v3", response["modules"])
        self.assertIn("causal_diagnostics", response["modules"])
        self.assertEqual(response["modules"]["anomaly_v3"]["status"], "available")
        response["screens"].clear()
        self.assertIn("return", original["screens"])
        stale = runtime.read(NOW + 91, shadow.read(NOW + 91))
        for name in ("health_score", "diagnostic_ai"):
            self.assertEqual(stale["modules"][name]["status"], "stale")
            self.assertIsNone(stale["modules"][name]["result"])
        self.assertIsNone(stale["screens"])


class UnifiedHTTPTests(unittest.TestCase):
    def test_get_is_ram_only_non_mutating_and_compatible_with_old_routes(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), api.Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def request(method, path):
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as client:
                    client.request(method, path)
                    response = client.getresponse()
                    return response.status, response.getheader("Cache-Control"), response.read()

            try:
                runtime = UnifiedRuntime()
                with (patch.object(api, "_intelligence_runtime", runtime),
                      patch.object(api, "_shadow_runtime", ShadowRuntime()),
                      patch.object(api, "snapshot", side_effect=AssertionError("upstream")) as poll,
                      patch.object(runtime, "refresh", side_effect=AssertionError("file I/O")) as refresh,
                      patch.object(api, "_load_fuel_engine", side_effect=AssertionError("fuel engine")) as fuel):
                    status, cache, body = request("GET", PATH)
                    self.assertEqual((status, cache), (200, "no-store"))
                    result = json.loads(body)
                    self.assertEqual(result["schema"], SCHEMA)
                    self.assertEqual(set(result["modules"]),
                                     {"burner_ml", "fuel_ml", "weather_impact", "health_score", "diagnostic_ai", "anomaly_v3", "causal_diagnostics"})
                    self.assertFalse(result["live_predictions_enabled"])
                    for name in ("burner_ml", "fuel_ml", "weather_impact"):
                        self.assertIsNone(result["modules"][name]["prediction"])
                    for method in ("POST", "DELETE", "PUT", "PATCH"):
                        self.assertIn(request(method, PATH)[0], (404, 405, 501))
                    self.assertEqual(json.loads(request("GET", SHADOW_PATH)[2])["schema"], "thermoflow-shadow/1")
                    for forbidden in (poll, refresh, fuel):
                        forbidden.assert_not_called()
                with patch.object(api, "_load_fuel_engine") as fuel:
                    fuel.return_value.handle.return_value = (200, {"configured": True, "warming": True})
                    status, _, body = request("GET", "/api/fuel/summary")
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), {"configured": True, "warming": True})
                    self.assertEqual(fuel.return_value.handle.call_args.args[:2], ("GET", "/api/fuel/summary"))
            finally:
                server.shutdown()
                worker.join(timeout=2)
