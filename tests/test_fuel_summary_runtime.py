"""Tests for summary worker lifecycle, cache and cold-start response contract."""
import threading
import unittest
from unittest.mock import patch

from fuel_errors import FuelError
from fuel_summary_runtime import get_summary, summary_build_profile, summary_build_stage, summary_job, summary_kick, warmup


class FakeThread:
    def __init__(self, target, name, daemon, run_on_start=True, alive_after_start=False):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.run_on_start = run_on_start
        self.alive_after_start = alive_after_start
        self.alive = False
        self.started = False
        self.join_timeout = None

    def start(self):
        self.started = True
        self.alive = True
        if self.run_on_start:
            self.target()
            self.alive = False
        elif not self.alive_after_start:
            self.alive = False

    def join(self, timeout=None):
        self.join_timeout = timeout

    def is_alive(self):
        return self.alive


class SummaryRuntimeTests(unittest.TestCase):
    def deps(self, **overrides):
        cache = {"at": 10.0, "data": None}
        state = {"thread": None, "error": None}
        calls = []
        threads = []
        def factory(**kwargs):
            thread = FakeThread(**kwargs)
            threads.append(thread)
            return thread
        deps = {
            "prefetch": lambda: calls.append("prefetch"),
            "build": lambda: calls.append("build") or cache.update(at=20.0, data={"ready": True}),
            "fuel_error": FuelError,
            "state": state,
            "guard": threading.Lock(),
            "thread_factory": factory,
            "configured": lambda: True,
            "cache": cache,
            "monotonic": lambda: 20.0,
            "cache_seconds": 60.0,
            "wait_seconds": 25.0,
        }
        deps.update(overrides)
        return deps, calls, threads

    def test_job_runs_prefetch_then_build_and_clears_error(self):
        deps, calls, _ = self.deps()
        deps["state"]["error"] = "old error"
        summary_job(deps)
        self.assertEqual(calls, ["prefetch", "build"])
        self.assertIsNone(deps["state"]["error"])

    def test_job_captures_fuel_and_unexpected_errors(self):
        deps, _, _ = self.deps(prefetch=lambda: (_ for _ in ()).throw(FuelError(503, "offline")))
        summary_job(deps)
        self.assertEqual(deps["state"]["error"], "offline")
        deps["prefetch"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        summary_job(deps)
        self.assertEqual(deps["state"]["error"], "RuntimeError: boom")

    def test_kick_reuses_live_thread_and_creates_daemon_worker(self):
        deps, _, threads = self.deps()
        deps["thread_factory"] = lambda **kwargs: (
            threads.append(FakeThread(**kwargs, run_on_start=False, alive_after_start=True)) or threads[-1]
        )
        first = summary_kick(deps)
        second = summary_kick(deps)
        self.assertIs(first, second)
        self.assertEqual(len(threads), 1)
        self.assertEqual(first.name, "fuel-summary")
        self.assertTrue(first.daemon)
        self.assertTrue(first.started)

    def test_unconfigured_and_fresh_cache_do_not_start_worker(self):
        deps, _, threads = self.deps(configured=lambda: False)
        self.assertEqual(get_summary(deps), {"configured": False})
        deps, _, threads = self.deps()
        deps["cache"].update(at=19.0, data={"cached": True})
        self.assertEqual(get_summary(deps), {"cached": True})
        self.assertEqual(threads, [])

    def test_stale_cache_is_returned_while_refresh_starts(self):
        deps, calls, threads = self.deps(monotonic=lambda: 100.0)
        deps["cache"].update(at=0.0, data={"stale": True})
        self.assertEqual(get_summary(deps), {"stale": True})
        self.assertEqual(len(threads), 1)
        self.assertEqual(calls, ["prefetch", "build"])

    def test_cold_start_waits_and_returns_new_cache(self):
        deps, calls, threads = self.deps()
        result = get_summary(deps)
        self.assertEqual(result, {"ready": True})
        self.assertEqual(calls, ["prefetch", "build"])
        self.assertEqual(threads[0].join_timeout, 25.0)

    def test_cold_start_failure_maps_to_502_and_timeout_returns_warming(self):
        deps, _, _ = self.deps(prefetch=lambda: (_ for _ in ()).throw(FuelError(503, "offline")))
        with self.assertRaises(FuelError) as caught:
            get_summary(deps)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.message, "offline")

        deps, _, threads = self.deps()
        deps["thread_factory"] = lambda **kwargs: FakeThread(
            **kwargs, run_on_start=False, alive_after_start=True
        )
        self.assertEqual(get_summary(deps), {"configured": True, "warming": True})
        self.assertEqual(threads, [])

    def test_warmup_only_starts_when_configured(self):
        deps, _, threads = self.deps(configured=lambda: False)
        warmup(deps)
        self.assertEqual(threads, [])
        deps["configured"] = lambda: True
        warmup(deps)
        self.assertEqual(len(threads), 1)

    def test_runtime_cold_start_wait_budget_and_prefetch_window(self):
        import thermoflow2_runtime as runtime

        deps, _, threads = self.deps(
            wait_seconds=runtime._summary_runtime_deps()["wait_seconds"]
        )

        def factory(**kwargs):
            thread = FakeThread(**kwargs, run_on_start=False, alive_after_start=True)
            threads.append(thread)
            return thread

        deps["thread_factory"] = factory
        self.assertEqual(get_summary(deps), {"configured": True, "warming": True})
        self.assertEqual(threads[0].join_timeout, 1.0)
        self.assertEqual(get_summary(deps), {"configured": True, "warming": True})
        self.assertEqual(len(threads), 1)
        # The same worker can still publish the full result after the request ends.
        threads[0].target()
        self.assertEqual(get_summary(deps), {"ready": True})

        with patch.object(runtime, "_prefetch") as prefetch:
            runtime._summary_runtime_deps()["prefetch"]()
        prefetch.assert_called_once_with(days=120)

    def test_slow_stage_log_identifies_phase_without_request_details(self):
        deps, _, _ = self.deps()
        with patch("fuel_summary_runtime.perf_counter", side_effect=[0, 12, 12, 13]):
            with self.assertLogs("fuel_summary_runtime", level="WARNING") as logs:
                summary_job(deps)
        self.assertEqual(logs.output, ["WARNING:fuel_summary_runtime:Fuel summary prefetch took 12.0s"])

    def test_profile_reports_exclusive_stage_times_and_counts(self):
        with patch("fuel_summary_runtime.perf_counter", side_effect=[0, 2, 3, 8, 10, 12, 22, 23, 24, 30]):
            with self.assertLogs("fuel_summary_runtime", level="WARNING") as logs:
                with summary_build_profile():
                    with summary_build_stage("calibration"):
                        with summary_build_stage("temperature_offset"):
                            pass
                    with summary_build_stage("telemetry_fetch"):
                        pass
                    with summary_build_stage("telemetry_fetch"):
                        pass
        self.assertEqual(logs.output, [
            "WARNING:fuel_summary_runtime:Fuel summary build profile status=ok total=30.000s "
            "temperature_offset=5.000s/1 calibration=3.000s/1 telemetry_fetch=11.000s/2 other=11.000s"
        ])

    def test_profile_marks_failed_build_and_resets_after_error(self):
        with self.assertLogs("fuel_summary_runtime", level="WARNING") as logs:
            with self.assertRaisesRegex(ValueError, "private details"):
                with summary_build_profile():
                    with summary_build_stage("calibration"):
                        raise ValueError("private details")
        self.assertIn("status=failed", logs.output[0])
        self.assertNotIn("private details", logs.output[0])
        with patch("fuel_summary_runtime.perf_counter") as clock:
            with summary_build_stage("calibration"):
                pass
        clock.assert_not_called()

    def test_runtime_profiles_real_summary_entry_points(self):
        import thermoflow2_runtime as runtime

        def fake_build(deps):
            deps["om_offset"]()
            deps["om_offset"]()  # Fresh cache: no second refresh is timed.
            deps["calibration_inputs"]([])
            self.assertEqual(runtime._telemetry_history(120), {"telemetry": "fixture"})
            return {"result": "unchanged"}

        with (
            patch("fuel_summary_service.build_summary", side_effect=fake_build),
            patch.object(runtime, "_offset_cache", {"at": 0.0, "data": None}),
            patch.object(runtime, "_temps_data_start", return_value=None),
            patch.object(runtime, "_periods", return_value=[]),
            patch.object(runtime, "_calibration", return_value={}),
            patch.object(runtime, "_counter_calibration_impl", return_value={}),
            patch.object(runtime, "fetch_telemetry_history", return_value={"telemetry": "fixture"}),
            self.assertLogs("fuel_summary_runtime", level="WARNING") as logs,
        ):
            self.assertEqual(runtime._summary_build(), {"result": "unchanged"})
        self.assertRegex(logs.output[0], r"temperature_offset=\d+\.\d+s/1 calibration=\d+\.\d+s/2 telemetry_fetch=\d+\.\d+s/1")


if __name__ == "__main__":
    unittest.main()
