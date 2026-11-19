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
            "day": f"2026-01-{(index % 28) + 1:02d}" if index < 28 else f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "kg_counter": kg,
            "hdd_lag": hdd,
            "solar_mj": solar,
            "wind_max_kmh": wind,
            "wind_hdd_lag": wind_hdd,
            "counter_regime": "new",
            "estimate": False,
        })
    # Ensure chronological order is unambiguous for the holdout split.
    for index, row in enumerate(rows):
        row["day"] = f"2026-01-{index + 1:03d}"
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
        self.assertEqual(result["holdout_from"], "2026-01-077")
        self.assertEqual(result["holdout_to"], "2026-01-090")
        self.assertIn("mae_kg_day", result["candidate"])
        self.assertIn("production estimates are unchanged", result["safety"])

    def test_provisional_and_epoch_days_are_excluded(self):
        rows = synthetic_rows()
        rows[0]["estimate"] = True
        rows[1]["counter_regime"] = "epoch"
        result = fuel_ai.evaluate(rows)
        self.assertEqual(result["training_days"], 88)


if __name__ == "__main__":
    unittest.main()
