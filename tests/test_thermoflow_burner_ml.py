import copy
import unittest

from thermoflow_burner_ml import evaluate_burner_model
from thermoflow_observed_cycles import FIRE, INPUT_SCHEMA, build_observed_cycle_dataset


def measured_shape(count, *, varied=True):
    """Generate regular sampling with independent duration and idle variation."""
    state, durations, idle = [], [], []
    for i in range(count):
        durations.append(120 + (i * i % 7) * 30 if varied else 120)
        idle.append(150 + (3 * i % 11) * 30 if varied else 150)
        state.extend([0] * (idle[-1] // 30) + [1] * (durations[-1] // 30))
    state.extend([0, 0])
    points = [[1_800_000_000 + i * 30, value] for i, value in enumerate(state)]
    return build_observed_cycle_dataset({
        "schema": INPUT_SCHEMA, "sampling": "raw", "points": {FIRE: points}})


class BurnerBenchmarkTests(unittest.TestCase):
    def test_fit_is_shadow_only_and_baselines_share_holdout(self):
        result = evaluate_burner_model(measured_shape(35))
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["promotion"], "disabled")
        evaluation = result["evaluation"]
        self.assertLess(evaluation["training_ends_at"], evaluation["holdout_starts_at"])
        self.assertEqual(evaluation["candidate"]["samples"], 5)
        self.assertEqual(evaluation["baselines"]["training_median"]["samples"], 5)
        self.assertEqual(evaluation["baselines"]["previous_cycle"]["samples"], 5)

    def test_changing_holdout_targets_does_not_change_fitted_model(self):
        original = measured_shape(35)
        changed = copy.deepcopy(original)
        for row in changed["dataset"]["holdout"]:
            row["target"] += 30
        left = evaluate_burner_model(original)
        right = evaluate_burner_model(changed)
        self.assertEqual(left["model"], right["model"])
        self.assertNotEqual(left["evaluation"]["candidate"]["mae_minutes"],
                            right["evaluation"]["candidate"]["mae_minutes"])

    def test_small_and_degenerate_datasets_do_not_publish_a_candidate(self):
        small = evaluate_burner_model(measured_shape(4))
        self.assertEqual(small["status"], "collecting")
        self.assertIsNone(small["model"])
        flat = evaluate_burner_model(measured_shape(35, varied=False))
        self.assertEqual(flat["reason"], "degenerate_training_features")
        self.assertIsNone(flat["model"])
        self.assertEqual(flat["evaluation"]["baselines"]["previous_cycle"]["mae_minutes"], 0)

    def test_rejects_unphysical_target(self):
        report = measured_shape(35)
        report["dataset"]["holdout"][-1]["target"] = -1
        with self.assertRaisesRegex(ValueError, "positive"):
            evaluate_burner_model(report)
