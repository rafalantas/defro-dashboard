import unittest
from unittest.mock import patch

import fuel_ai


def synthetic_rows(count=90):
    rows = []
    for index in range(count):
        hdd = float((index * 7) % 24)
        solar = float((index * 13) % 28)
        wind = float(4 + (index * 11) % 35)
        wind_hdd = wind * hdd / 100
        kg = max(0.0, 1.8 + 0.62 * hdd - 0.035 * solar + 0.42 * wind_hdd)
        rows.append({
            "day": f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "kg_counter": kg,
            "hdd_lag": hdd,
            "solar_mj": solar,
            "wind_max_kmh": wind,
            "wind_hdd_lag": wind_hdd,
            "counter_regime": "new",
            "estimate": False,
        })
    return rows


def add_telemetry(rows):
    for index, row in enumerate(rows):
        row.update({
            "telemetry_co_c": 65.0 + (index * 3) % 9,
            "telemetry_dhw_c": 42.0 + (index * 5) % 8,
            "telemetry_floor_c": 29.0 + (index * 7) % 8,
            "telemetry_return_c": 55.0 + (index * 11) % 12,
            "telemetry_boiler_power_kw": 3.0 + (index * 13) % 15,
            "telemetry_valve_opening_pct": 35.0 + (index * 17) % 55,
            "telemetry_pump_floor_h": 8.0 + (index * 3) % 12,
            "telemetry_pump_dhw_h": 1.0 + (index * 2) % 5,
            "telemetry_pump_circulation_h": 2.0 + (index * 5) % 7,
        })
    return rows


class FuelAiTests(unittest.TestCase):
    def test_insufficient_history_stays_collecting(self):
        result = fuel_ai.evaluate(synthetic_rows(50))
        self.assertEqual(result["status"], "collecting")
        self.assertNotIn("model", result)

    def test_shadow_model_uses_chronological_holdout(self):
        result = fuel_ai.evaluate(synthetic_rows())
        self.assertEqual(result["status"], "shadow_ready")
        self.assertEqual(result["mode"], "shadow_only")
        self.assertEqual(result["holdout_days"], 14)
        self.assertEqual(result["holdout_from"], "2026-03-21")
        self.assertEqual(result["holdout_to"], "2026-04-06")
        self.assertIn("mae_kg_day", result["candidate"])
        self.assertIn("production estimates are unchanged", result["safety"])
        self.assertEqual(result["validation"]["protocol"], "walk_forward_expanding_window")
        self.assertIn("fixed_holdout", result["validation"])
        self.assertTrue(result["ablation"]["available"])
        self.assertEqual(
            set(result["ablation"]["fixed_holdout"]),
            {"hdd_only", "hdd_plus_solar", "hdd_solar_wind_interaction"},
        )
        self.assertIn("not independent evidence", result["ablation"]["method"])
        fixed = result["validation"]["fixed_holdout"]
        self.assertEqual(fixed["scored_from"], result["holdout_from"])
        self.assertEqual(fixed["scored_to"], result["holdout_to"])
        self.assertLess(fixed["training_to"], fixed["scored_from"])
        self.assertIn("mae_kg_day", fixed["candidate"])

    def test_coefficient_stability_reports_expanding_windows(self):
        result = fuel_ai.evaluate(synthetic_rows(130))
        stability = result["coefficient_stability"]
        self.assertTrue(stability["available"])
        self.assertEqual(len(stability["windows"]), 3)
        sizes = [window["training_days"] for window in stability["windows"]]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(set(stability["features"]), set(result["features"]))
        for feature in stability["features"].values():
            self.assertEqual(len(feature["values"]), 3)
            self.assertIn("sign_consistent", feature)

    def test_coefficient_stability_is_unavailable_for_short_training_history(self):
        result = fuel_ai.evaluate(synthetic_rows(60))
        self.assertFalse(result["coefficient_stability"]["available"])
        self.assertEqual(
            result["coefficient_stability"]["reason"],
            "insufficient_history_for_three_expanding_windows",
        )

    def test_seven_days_of_telemetry_are_ingested_but_not_overfit(self):
        result = fuel_ai.evaluate(add_telemetry(synthetic_rows(7)))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["telemetry"]["status"], "collecting")
        self.assertEqual(result["telemetry"]["complete_days"], 7)
        self.assertGreater(result["telemetry"]["days_needed"], 7)

    def test_telemetry_candidate_uses_chronological_holdout_when_history_is_sufficient(self):
        result = fuel_ai.evaluate(add_telemetry(synthetic_rows(130)))
        self.assertEqual(result["telemetry"]["status"], "shadow_ready")
        self.assertEqual(result["telemetry"]["mode"], "shadow_only")
        self.assertGreaterEqual(result["telemetry"]["training_days"], fuel_ai.MIN_TRAIN_DAYS)
        self.assertGreaterEqual(result["telemetry"]["holdout_days"], fuel_ai.MIN_TEST_DAYS)
        self.assertIn("mae_kg_day", result["telemetry"]["baseline"])
        self.assertIn("mae_kg_day", result["telemetry"]["candidate"])
        self.assertIn("fixed_holdout", result["telemetry"]["validation"])
        self.assertIn("mae_kg_day", result["telemetry"]["validation"]["fixed_holdout"]["candidate"])
        self.assertTrue(result["telemetry"]["ablation"]["available"])
        self.assertEqual(
            set(result["telemetry"]["ablation"]["fixed_holdout"]),
            {
                "weather_only",
                "weather_plus_temperatures",
                "weather_plus_boiler_and_valve",
                "weather_plus_pumps",
                "weather_plus_all_telemetry",
            },
        )
        self.assertIn("not independent evidence", result["telemetry"]["ablation"]["method"])
        self.assertEqual(
            result["telemetry"]["ablation"]["fixed_holdout"]["weather_only"]["samples"],
            result["telemetry"]["holdout_days"],
        )
        self.assertGreaterEqual(result["telemetry"]["training_days"], fuel_ai.TELEMETRY_MIN_TRAIN_DAYS)
        self.assertGreaterEqual(result["telemetry"]["holdout_days"], fuel_ai.TELEMETRY_MIN_TEST_DAYS)


    def test_non_finite_and_negative_inputs_are_rejected(self):
        rows = synthetic_rows()
        rows[0]["solar_mj"] = float("nan")
        rows[1]["wind_max_kmh"] = float("inf")
        rows[2]["kg_counter"] = -1
        rows[3]["hdd_lag"] = "not-a-number"
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["rejected_days"], 4)
        self.assertEqual(result["training_days"], 86)

    def test_telemetry_values_outside_physical_ranges_are_excluded(self):
        rows = add_telemetry(synthetic_rows(130))
        rows[0]["telemetry_valve_opening_pct"] = 140
        rows[1]["telemetry_pump_floor_h"] = 25
        rows[2]["telemetry_return_c"] = float("nan")
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["telemetry"]["complete_days"], 127)
        self.assertEqual(result["telemetry"]["status"], "shadow_ready")

    def test_fixed_holdout_must_also_improve_before_weather_model_is_accepted(self):
        rows = synthetic_rows()
        with patch.object(fuel_ai, "_fixed_holdout_metrics", side_effect=[
            {"mae_kg_day": 10.0, "rmse_kg_day": 12.0, "bias_kg_day": 0.0, "samples": 14},
            {"mae_kg_day": 11.0, "rmse_kg_day": 13.0, "bias_kg_day": 0.0, "samples": 14},
            {"mae_kg_day": 10.0, "rmse_kg_day": 12.0, "bias_kg_day": 0.0, "samples": 14},
            {"mae_kg_day": 10.0, "rmse_kg_day": 12.0, "bias_kg_day": 0.0, "samples": 14},
            {"mae_kg_day": 10.0, "rmse_kg_day": 12.0, "bias_kg_day": 0.0, "samples": 14},
        ]):
            result = fuel_ai.evaluate(rows)
        self.assertGreaterEqual(result["mae_gain_percent"], fuel_ai.MIN_RELATIVE_GAIN * 100)
        self.assertLess(result["fixed_holdout_mae_gain_percent"], 0)
        self.assertFalse(result["accepted_weather_gain"])
        self.assertEqual(result["selected"], "temperature_only")

    def test_room_temperature_never_changes_fuel_model(self):
        rows = synthetic_rows()
        with_room_zero = [dict(row, room_c=0) for row in rows]
        with_room_valid = [dict(row, room_c=21.1) for row in rows]
        zero_result = fuel_ai.evaluate(with_room_zero)
        valid_result = fuel_ai.evaluate(with_room_valid)
        baseline_result = fuel_ai.evaluate(rows)
        self.assertEqual(zero_result["features"], baseline_result["features"])
        self.assertEqual(valid_result["features"], baseline_result["features"])
        self.assertEqual(zero_result["candidate"], baseline_result["candidate"])
        self.assertEqual(valid_result["candidate"], baseline_result["candidate"])

    def test_provisional_and_epoch_days_are_excluded(self):
        rows = synthetic_rows()
        rows[0]["estimate"] = True
        rows[1]["counter_regime"] = "epoch"
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["training_days"], 88)


    def test_invalid_calendar_dates_are_rejected(self):
        rows = synthetic_rows()
        rows[0]["day"] = "2026-02-30"
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["rejected_by_reason"]["invalid_base"], 1)
        self.assertEqual(result["training_days"], 89)

    def test_invalid_base_rows_report_the_specific_first_failure(self):
        rows = synthetic_rows(90)
        rows[0]["kg_counter"] = None
        rows[1]["hdd_lag"] = float("nan")
        rows[2]["solar_mj"] = None
        rows[3]["wind_max_kmh"] = -1
        rows[4]["day"] = "2026-02-30"
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["rejected_by_reason"]["invalid_base"], 5)
        self.assertEqual(result["invalid_base_by_reason"], {
            "kg_counter_missing_or_invalid": 1,
            "hdd_lag_missing_or_invalid": 1,
            "solar_mj_missing_or_invalid": 1,
            "wind_max_kmh_negative": 1,
            "day_missing_or_invalid": 1,
        })
        self.assertEqual(result["training_days"], 85)

    def test_counter_day_diagnostics_are_returned_for_missing_counter_rows(self):
        rows = synthetic_rows(50)
        rows[0]["kg_counter"] = None
        rows[0]["counter_diagnostics"] = {
            "day": rows[0]["day"],
            "reason": "counter_sample_gap_over_15m",
            "sample_count": 120,
            "first_sample_at": "2026-01-01T00:00:00+01:00",
            "last_sample_at": "2026-01-01T23:59:00+01:00",
            "start_counter_pct": 90.0,
            "end_counter_pct": 80.0,
            "drop_percent": None,
        }
        result = fuel_ai.evaluate(rows)
        self.assertEqual(len(result["counter_day_diagnostics"]), 1)
        self.assertEqual(
            result["counter_day_diagnostics"][0]["reason"],
            "counter_sample_gap_over_15m",
        )
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["rejected_days"], 1)
        self.assertEqual(result["invalid_base_by_reason"]["kg_counter_missing_or_invalid"], 1)

    def test_duplicate_days_are_excluded_instead_of_double_weighted(self):
        rows = synthetic_rows()
        duplicate = dict(rows[10])
        rows.append(duplicate)
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["rejected_by_reason"]["duplicate_day"], 2)
        self.assertEqual(result["training_days"], 89)

    def test_incomplete_telemetry_days_are_reported_separately(self):
        rows = add_telemetry(synthetic_rows(130))
        rows[0].pop("telemetry_return_c")
        rows[1].pop("telemetry_pump_floor_h")
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["telemetry_incomplete_days"], 2)
        self.assertEqual(result["telemetry"]["incomplete_days"], 2)


if __name__ == "__main__":
    unittest.main()
