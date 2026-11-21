import copy
import unittest
from datetime import datetime, timedelta, timezone

from thermoflow_fuel_ml import INPUT_SCHEMA, evaluate_fuel_model, prepare_fuel_dataset

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(count=33):
    periods = []
    for i in range(count):
        start = START + timedelta(days=i)
        periods.append({
            "start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat(),
            "refilled_kg": 8.0 + i * 0.1 + i % 5,
            "mass_reference": "verified_equal_fill_level",
            "baseline_kg_per_day": 10.0 + (i * i % 7) / 2,
            "baseline_issued_at": (start - timedelta(minutes=5)).isoformat(),
        })
    return {"schema": INPUT_SCHEMA, "calibration_epochs": [], "periods": periods}


class FuelMlTests(unittest.TestCase):
    def test_frozen_baseline_and_candidate_score_same_unseen_periods(self):
        doc = snapshot()
        doc["periods"].reverse()
        result = evaluate_fuel_model(doc)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["promotion"], "disabled")
        self.assertEqual(result["dataset"]["eligible_samples"], 32)
        self.assertTrue(all(item["samples"] == 5 for item in result["evaluation"].values()))
        self.assertLess(result["dataset"]["training"][-1]["timestamp"],
                        result["dataset"]["holdout"][0]["timestamp"])

    def test_partial_refills_and_epoch_crossings_do_not_supply_labels(self):
        doc = snapshot(8)
        doc["periods"][2]["mass_reference"] = "partial_refill"
        doc["calibration_epochs"].append((START + timedelta(days=4, hours=12)).isoformat())
        result = prepare_fuel_dataset(doc)
        self.assertEqual(result["excluded_periods_by_reason"], {
            "crosses_calibration_epoch": 1, "no_adjacent_verified_period_in_regime": 3,
            "unverified_mass_balance": 1,
        })
        self.assertEqual(result["dataset"]["eligible_samples"], 3)

    def test_exact_epoch_boundary_does_not_allow_previous_rate_from_old_regime(self):
        doc = snapshot(6)
        doc["calibration_epochs"].append(doc["periods"][2]["start"])
        result = prepare_fuel_dataset(doc)
        self.assertEqual(result["excluded_periods_by_reason"], {
            "no_adjacent_verified_period_in_regime": 2,
        })
        self.assertEqual(result["dataset"]["eligible_samples"], 4)

    def test_duplicate_intervals_are_rejected_as_a_group(self):
        doc = snapshot(8)
        doc["periods"].append(dict(doc["periods"][3]))
        result = prepare_fuel_dataset(doc)
        self.assertEqual(result["excluded_periods_by_reason"]["overlapping_or_duplicate_interval"], 2)
        self.assertEqual(result["dataset"]["eligible_samples"], 5)

    def test_late_baseline_cannot_enter_features_and_short_history_cannot_train(self):
        doc = snapshot(5)
        doc["periods"][2]["baseline_issued_at"] = doc["periods"][2]["end"]
        result = evaluate_fuel_model(doc)
        self.assertEqual(result["excluded_periods_by_reason"]["missing_invalid_or_late_baseline"], 1)
        self.assertEqual(result["dataset"]["eligible_samples"], 3)
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["model"])

    def test_holdout_label_change_does_not_change_fit(self):
        doc = snapshot()
        changed = copy.deepcopy(doc)
        changed["periods"][-1]["refilled_kg"] += 5
        left, right = evaluate_fuel_model(doc), evaluate_fuel_model(changed)
        self.assertEqual(left["model"], right["model"])
        self.assertNotEqual(left["evaluation"]["candidate"]["mae_kg_per_day"],
                            right["evaluation"]["candidate"]["mae_kg_per_day"])

    def test_current_regime_does_not_borrow_training_rows(self):
        doc = snapshot()
        doc["calibration_epochs"].append(doc["periods"][25]["start"])
        result = evaluate_fuel_model(doc)
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["excluded_periods_by_reason"]["prior_calibration_epoch"], 25)
        self.assertEqual(result["dataset"]["eligible_samples"], 7)
