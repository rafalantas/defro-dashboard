import unittest
from thermoflow_data_quality_gates import (inspect_fire_snapshot, inspect_fuel_snapshot,
    inspect_weather_forecasts, build_data_quality_report)

class DataQualityGateTests(unittest.TestCase):
    def test_fire_requires_binary_unique_continuous_complete_cycles(self):
        good = {"schema":"thermoflow-raw-fire/1","sampling":"raw",
                "points":{"thermoflow-fire":[[100,0],[130,0],[160,1],[190,1],[220,0]]}}
        self.assertEqual(inspect_fire_snapshot(good)["status"], "ready")
        bad = {"schema":"thermoflow-raw-fire/1","sampling":"raw",
               "points":{"thermoflow-fire":[[100,0],[130,1],[130,0],[300,1]]}}
        result = inspect_fire_snapshot(bad)
        self.assertEqual(result["status"], "blocked")
        self.assertGreater(result["gaps_over_limit"], 0)

    def test_fuel_requires_verified_labels_and_as_issued_baselines(self):
        base = {"schema":"thermoflow-verified-fuel-periods/1",
                "label_mode":"verified_equal_fill_level","baseline_mode":"as_issued",
                "refills":[{"ids":[1,2]},{"ids":[3,4]}],
                "periods":[{"start":"2026-11-01T00:00:00+00:00","end":"2026-11-02T00:00:00+00:00",
                    "refilled_kg":20,"baseline_kg_per_day":18,
                    "baseline_issued_at":"2026-10-31T23:00:00+00:00"}]}
        self.assertEqual(inspect_fuel_snapshot(base, verified_refill_ids=[1,2,3,4])["status"], "ready")
        base["baseline_mode"] = "replay"
        self.assertEqual(inspect_fuel_snapshot(base)["status"], "blocked")

    def test_period_overlap_and_future_baseline_are_blockers(self):
        snap = {"schema":"thermoflow-verified-fuel-periods/1","label_mode":"verified_equal_fill_level",
            "baseline_mode":"as_issued","refills":[],
            "periods":[
                {"start":"2026-11-01T00:00:00+00:00","end":"2026-11-03T00:00:00+00:00","refilled_kg":10,"baseline_kg_per_day":4,"baseline_issued_at":"2026-11-01T01:00:00+00:00"},
                {"start":"2026-11-02T00:00:00+00:00","end":"2026-11-04T00:00:00+00:00","refilled_kg":10,"baseline_kg_per_day":4,"baseline_issued_at":"2026-11-01T00:00:00+00:00"}]}
        self.assertEqual(inspect_fuel_snapshot(snap)["status"], "blocked")

    def test_weather_requires_exact_as_issued_forecast_per_period(self):
        snap = {"periods":[{"start":"2026-11-01T00:00:00+00:00","end":"2026-11-02T00:00:00+00:00"}],
            "forecasts":[{"start":"2026-11-01T00:00:00+00:00","end":"2026-11-02T00:00:00+00:00",
                "issued_at":"2026-10-31T20:00:00+00:00","available_at":"2026-10-31T20:01:00+00:00",
                "kind":"forecast","forecast_mean_outdoor_c":2.5}]}
        self.assertEqual(inspect_weather_forecasts(snap)["status"], "ready")
        snap["forecasts"][0]["available_at"] = "2026-11-01T00:01:00+00:00"
        self.assertEqual(inspect_weather_forecasts(snap)["status"], "blocked")

    def test_combined_report_is_advisory_only(self):
        report = build_data_quality_report(fire={"schema":"bad"}, fuel={"schema":"bad"})
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["promotion"], "disabled")

if __name__ == "__main__":
    unittest.main()
