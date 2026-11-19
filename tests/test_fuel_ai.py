import unittest

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
        fixed = result["validation"]["fixed_holdout"]
        self.assertEqual(fixed["scored_from"], result["holdout_from"])
        self.assertEqual(fixed["scored_to"], result["holdout_to"])
        self.assertLess(fixed["training_to"], fixed["scored_from"])
        self.assertIn("mae_kg_day", fixed["candidate"])

    def test_seven_days_of_telemetry_are_ingested_but_not_overfit(self):
        result = fuel_ai.evaluate(add_telemetry(synthetic_rows(7)))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["telemetry"]["status"], "collecting")
        self.assertEqual(result["telemetry"]["complete_days"], 7)
        self.assertGreater(result["telemetry"]["days_needed"], 7)

    def test_telemetry_candidate_uses_chronological_holdout_when_history_is_sufficient(self):
        result = fuel_ai.evaluate(add_telemetry(synthetic_rows(100)))
        self.assertEqual(result["telemetry"]["status"], "shadow_ready")
        self.assertEqual(result["telemetry"]["mode"], "shadow_only")
        self.assertGreaterEqual(result["telemetry"]["training_days"], fuel_ai.MIN_TRAIN_DAYS)
        self.assertGreaterEqual(result["telemetry"]["holdout_days"], fuel_ai.MIN_TEST_DAYS)
        self.assertIn("mae_kg_day", result["telemetry"]["baseline"])
        self.assertIn("mae_kg_day", result["telemetry"]["candidate"])
        self.assertIn("fixed_holdout", result["telemetry"]["validation"])
        self.assertIn("mae_kg_day", result["telemetry"]["validation"]["fixed_holdout"]["candidate"])

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


if __name__ == "__main__":
    unittest.main()
