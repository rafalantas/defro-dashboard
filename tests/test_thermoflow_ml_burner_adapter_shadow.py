import unittest
from datetime import datetime, timezone

from thermoflow_ml_burner_adapter import (
    FEATURE_KEYS,
    prepare_burner_cycle_dataset,
    train_burner_cycle_model,
)


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class ThermoFlowMlBurnerAdapterTests(unittest.TestCase):
    def cycles(self, count=26):
        cycle_start = 1_800_000_000
        complete = []
        for index in range(count):
            duration = 20.0 + (index % 5) * 2.0
            complete.append({
                "start": iso(cycle_start),
                "end": iso(cycle_start + int(duration * 60)),
                "duration_minutes": duration,
                "complete": True,
                "status": "confirmed_by_fire_state",
            })
            # Vary idle intervals independently of burn duration so the
            # predictors are not perfectly collinear in this fixture.
            cycle_start += 3600 + (index % 4) * 300
        return {"cycles": complete, "incomplete_cycles": []}

    def test_maps_real_diagnostic_cycle_contract_to_supervised_rows(self):
        result = prepare_burner_cycle_dataset(self.cycles())
        self.assertEqual(result["source"], "thermoflow_fire_confirmed_burner_cycles")
        self.assertEqual(result["feature_keys"], list(FEATURE_KEYS))
        self.assertEqual(result["target_key"], "duration_minutes")
        self.assertEqual(result["eligible_samples"], 25)
        self.assertEqual(result["training_samples"], 20)
        self.assertEqual(result["holdout_samples"], 5)
        self.assertEqual(result["status"], "ready_for_shadow_training")

    def test_incomplete_cycle_breaks_previous_cycle_link(self):
        cycles = self.cycles()
        middle = cycles["cycles"][10]
        cycles["incomplete_cycles"] = [{
            "start": middle["start"], "end": middle["end"], "complete": False,
            "reason": "telemetry_gap",
        }]
        result = prepare_burner_cycle_dataset(cycles)
        self.assertLess(result["eligible_samples"], 25)

    def test_overlapping_cycles_are_not_joined(self):
        cycles = self.cycles()
        cycles["cycles"][4]["start"] = cycles["cycles"][3]["start"]
        result = prepare_burner_cycle_dataset(cycles)
        self.assertGreaterEqual(result["adapter_rejected_cycles"]["overlapping_cycles"], 1)

    def test_invalid_and_censored_cycles_are_excluded(self):
        cycles = self.cycles()
        cycles["cycles"][0]["duration_minutes"] = None
        cycles["incomplete_cycles"] = [{
            "start": iso(1_800_100_000), "end": iso(1_800_100_300),
            "complete": False, "reason": "right_censored_open_cycle",
        }]
        result = prepare_burner_cycle_dataset(cycles)
        self.assertGreater(result["adapter_rejected_cycles"]["malformed_cycle"], 0)
        self.assertEqual(result["status"], "collecting")

    def test_end_to_end_shadow_training_uses_burner_cycles(self):
        result = train_burner_cycle_model(self.cycles())
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["model"]["training_samples"], 20)
        self.assertEqual(result["model"]["holdout_samples"], 5)
        self.assertEqual(result["promotion"], "disabled_shadow_only")
        self.assertEqual(result["model_purpose"], "experimental_burner_cycle_duration_estimate")

    def test_small_history_stays_in_collecting_state(self):
        result = train_burner_cycle_model(self.cycles(20))
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["model"])


if __name__ == "__main__":
    unittest.main()
