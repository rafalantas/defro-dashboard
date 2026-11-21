"""Contract tests for extracted daily endpoint orchestration."""
import threading
import unittest

from fuel_daily_service import build_daily
from fuel_errors import FuelError


class DailyServiceTests(unittest.TestCase):
    def dependencies(self, **overrides):
        calls = []
        rows = [{
            "day": "2026-11-19", "t_mean": 5.0, "kg_counter": 2.5,
            "telemetry_status": "available",
        }]
        deps = {
            "max_days": 120,
            "fuel_error": FuelError,
            "prefetch": lambda days: calls.append(("prefetch", days)),
            "lock": threading.Lock(),
            "refills": lambda: calls.append(("refills",)) or [{"id": 1}],
            "fit_model": lambda refills: calls.append(("fit_model", len(refills))) or {
                "base_kg_per_day": 0.2
            },
            "daily_rows": lambda days: calls.append(("daily_rows", days)) or (
                [dict(row) for row in rows], {"collecting": True}
            ),
            "fit_daily_model": lambda values: calls.append(("fit_daily_model", len(values))) or {
                "status": "ready"
            },
            "predict_kg_per_day": lambda model, temperature: 1.234,
            "telemetry_state": lambda values: {"status": "available", "days": len(values)},
        }
        deps.update(overrides)
        return deps, calls

    def test_invalid_days_preserves_400_contract_without_prefetch(self):
        deps, calls = self.dependencies()
        with self.assertRaises(FuelError) as caught:
            build_daily({"days": ["bad"]}, deps)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.message, "days musi być liczbą")
        self.assertEqual(calls, [])

    def test_payload_and_prediction_contract(self):
        deps, calls = self.dependencies()
        result = build_daily({"days": ["30"]}, deps)
        self.assertEqual(
            set(result),
            {"model", "daily_model", "counter_calibration", "telemetry", "weather_vars", "days"},
        )
        self.assertEqual(calls[0], ("prefetch", 32))
        self.assertEqual(result["days"][0]["model_kg"], 1.2)
        self.assertEqual(result["days"][0]["residual_kg"], 1.3)
        self.assertEqual(result["counter_calibration"], {"collecting": True})
        self.assertEqual(result["telemetry"]["status"], "available")
        self.assertEqual(result["weather_vars"]["solar_mj"], "suma promieniowania krótkofalowego, MJ/m²")

    def test_network_bound_daily_rows_run_outside_shared_lock(self):
        deps, _ = self.dependencies()
        lock = deps["lock"]
        deps["daily_rows"] = lambda days: self.assertFalse(lock.locked()) or (
            [{"day": "2026-11-19", "t_mean": 5.0, "kg_counter": None}], {}
        )
        build_daily({"days": ["1"]}, deps)

    def test_no_model_leaves_rows_without_model_fields(self):
        deps, _ = self.dependencies(fit_model=lambda refills: None)
        result = build_daily({"days": ["1"]}, deps)
        self.assertNotIn("model_kg", result["days"][0])
        self.assertNotIn("residual_kg", result["days"][0])

    def test_prefetch_failure_propagates_before_locked_calculations(self):
        def fail(_days):
            raise FuelError(502, "prefetch failed")
        deps, calls = self.dependencies(prefetch=fail)
        with self.assertRaisesRegex(FuelError, "prefetch failed"):
            build_daily({"days": ["1"]}, deps)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
