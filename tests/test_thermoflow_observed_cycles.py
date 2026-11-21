import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from thermoflow_diagnostic_intelligence import segment_burner_cycles
from thermoflow_observed_cycles import FIRE, INPUT_SCHEMA, build_observed_cycle_dataset

T0 = 1_800_000_000


def samples(count, start=T0):
    return [[start + 300 * cycle + offset, value]
            for cycle in range(count)
            for offset, value in ((0, 0), (60, 1), (120, 1), (180, 0), (240, 0))]


def snapshot(points):
    return {"schema": INPUT_SCHEMA, "sampling": "raw", "points": {FIRE: points}}


class ObservedCycleTests(unittest.TestCase):
    def test_full_snapshot_has_exact_cycle_targets_and_ordered_holdout(self):
        original = snapshot(list(reversed(samples(110))))
        result = build_observed_cycle_dataset(original)
        data = result["dataset"]
        self.assertEqual(result["counts"]["complete"], 110)
        self.assertEqual(len(result["cycles"]), 110)
        self.assertEqual(len(segment_burner_cycles(original["points"])["cycles"]), 100)
        self.assertEqual(data["training_samples"], 104)
        self.assertEqual(data["holdout_samples"], 5)
        self.assertLess(data["training"][-1]["timestamp"], data["holdout"][0]["timestamp"])
        self.assertEqual(data["training"][0]["features"],
                         {"previous_duration_minutes": 2.0, "idle_minutes": 3.0})
        self.assertEqual(data["training"][0]["target"], 2.0)
        self.assertEqual(original["points"][FIRE], list(reversed(samples(110))))

    def test_unknown_idle_across_gap_cannot_link_two_valid_cycles(self):
        result = build_observed_cycle_dataset(snapshot(samples(2) + samples(2, T0 + 1200)))
        self.assertEqual(result["counts"]["complete"], 4)
        self.assertEqual(result["counts"]["segments"], 2)
        self.assertEqual(result["dataset"]["eligible_samples"], 2)

    def test_initial_on_and_open_before_gap_are_censored(self):
        result = build_observed_cycle_dataset(snapshot([
            [T0, 1], [T0 + 60, 1], [T0 + 120, 0], [T0 + 180, 0],
            [T0 + 240, 1], [T0 + 300, 1],
            [T0 + 1000, 0], [T0 + 1060, 1], [T0 + 1120, 1],
        ]))
        self.assertEqual(result["counts"]["complete"], 0)
        self.assertEqual([item["reason"] for item in result["incomplete_cycles"]],
                         ["left_censored_initial_on_state", "telemetry_gap",
                          "right_censored_open_cycle"])

    def test_bad_or_duplicate_fire_timestamp_breaks_idle_coverage(self):
        points = samples(2) + [[T0 + 270, 0], [T0 + 270, 7]]
        result = build_observed_cycle_dataset(snapshot(points))
        self.assertEqual(result["counts"]["complete"], 2)
        self.assertEqual(result["counts"]["segments"], 2)
        self.assertEqual(result["dataset"]["eligible_samples"], 0)
        self.assertEqual(result["input_quality"][FIRE]["duplicate_timestamp_samples"], 2)
        self.assertEqual(result["input_quality"][FIRE]["invalid_value"], 1)

    def test_flue_only_does_not_create_a_label_and_compact_input_is_rejected(self):
        doc = snapshot([])
        doc["points"]["thermoflow-flue"] = [[T0, 100]]
        result = build_observed_cycle_dataset(doc)
        self.assertEqual(result["counts"]["complete"], 0)
        self.assertEqual(result["dataset"]["status"], "collecting")
        with self.assertRaisesRegex(ValueError, "sampling"):
            build_observed_cycle_dataset({**doc, "sampling": "compact"})

    def test_cli_outputs_verifiable_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot(samples(2))), encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "thermoflow_observed_cycles", str(path)],
                                 capture_output=True, text=True, check=True)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["counts"]["complete"], 2)
        self.assertRegex(payload["input_sha256"], "^[a-f0-9]{64}$")
