import unittest

from fuel_refill_ai import TELEMETRY_FEATURES
from fuel_thermoflow_diagnostics import build_diagnostics


class ThermoFlowDiagnosticsTests(unittest.TestCase):
    def test_reports_missing_telemetry_by_feature_and_period(self):
        complete = {name: 1.0 for name in TELEMETRY_FEATURES}
        rows = [
            dict(complete, day="2026-01-01", end="2026-01-01T12:00:00+01:00",
                 telemetry_complete=True, telemetry_missing_features=[]),
            {"day": "2026-01-02", "end": "2026-01-02T12:00:00+01:00",
             "telemetry_complete": False,
             "telemetry_missing_features": ["telemetry_return_c", "telemetry_pump_floor_h"]},
        ]
        result = build_diagnostics(rows, {"status": "collecting"})
        quality = result["telemetry_quality"]
        self.assertEqual(quality["periods_total"], 2)
        self.assertEqual(quality["periods_complete"], 1)
        self.assertEqual(quality["periods_incomplete"], 1)
        self.assertEqual(quality["missing_by_feature"]["telemetry_return_c"], 1)
        self.assertEqual(quality["missing_by_feature"]["telemetry_pump_floor_h"], 1)
        self.assertEqual(quality["feature_coverage_periods"]["telemetry_return_c"], 1)
        self.assertEqual(quality["feature_coverage_periods"]["telemetry_pump_floor_h"], 1)
        self.assertEqual(quality["feature_coverage_percent"]["telemetry_return_c"], 50.0)
        self.assertEqual(quality["coverage_basis"], "accepted_refill_period_aggregates")
        self.assertEqual(quality["missing_by_period"][0]["day"], "2026-01-02")

    def test_actual_missing_values_override_stale_empty_missing_metadata(self):
        rows = [
            {name: 1.0 for name in TELEMETRY_FEATURES},
            {**{name: 1.0 for name in TELEMETRY_FEATURES},
             "telemetry_floor_c": None, "telemetry_missing_features": []},
        ]
        quality = build_diagnostics(rows, {"status": "collecting"})["telemetry_quality"]
        self.assertEqual(quality["missing_by_feature"]["telemetry_floor_c"], 1)
        self.assertEqual(quality["feature_coverage_periods"]["telemetry_floor_c"], 1)
        self.assertEqual(quality["periods_complete"], 1)

    def test_empty_input_has_null_percentages_and_zero_period_coverage(self):
        quality = build_diagnostics([], {"status": "collecting"})["telemetry_quality"]
        self.assertEqual(quality["feature_coverage_periods"]["telemetry_floor_c"], 0)
        self.assertIsNone(quality["feature_coverage_percent"]["telemetry_floor_c"])

    def test_exposes_chronological_period_errors_and_model_comparison(self):
        comparison = [{"period_end": "2026-01-03T12:00:00+01:00",
                       "actual_kg_day": 20.0, "baseline_error_kg_day": 2.0}]
        ai = {
            "status": "shadow_ready", "selected": "weather_aware",
            "baseline": {"mae_kg_day": 3.0}, "candidate": {"mae_kg_day": 2.0},
            "mae_gain_percent": 33.3,
            "validation": {"periods": comparison},
            "telemetry": {"status": "collecting", "periods_needed": 25},
        }
        result = build_diagnostics([], ai)
        self.assertEqual(result["refill_forecast_accuracy"]["period_errors"], comparison)
        self.assertEqual(result["model_comparison"]["status"], "available")
        self.assertEqual(result["model_comparison"]["promotion"], "disabled_shadow_only")

    def test_flags_low_mean_return_without_claiming_a_cycle_or_control_action(self):
        rows = [{
            "day": "2026-01-01", "end": "2026-01-02T12:00:00+01:00",
            "telemetry_return_c": 53.5,
        }]
        result = build_diagnostics(rows, {"status": "collecting"})
        anomaly = result["boiler_anomaly_screen"]
        self.assertEqual(anomaly["flags_count"], 1)
        self.assertEqual(anomaly["flags"][0]["code"], "return_mean_below_protection_margin")
        self.assertEqual(anomaly["cycle_detection"], "requires_timestamped_raw_telemetry")
        self.assertIn("55°C", anomaly["note"])


if __name__ == "__main__":
    unittest.main()
