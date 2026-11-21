import unittest

from thermoflow_ml_dataset import (
    DEFAULT_HOLDOUT_SAMPLES,
    DEFAULT_MIN_TRAINING_SAMPLES,
    SCHEMA_VERSION,
    prepare_chronological_dataset,
)


class ThermoFlowMlDatasetTests(unittest.TestCase):
    def make_samples(self, count=25):
        return [
            {"timestamp": index + 1, "complete": True, "duration": 10 + index,
             "mean_return": 55 + index / 10}
            for index in range(count)
        ]

    def prepare(self, samples):
        return prepare_chronological_dataset(
            samples, feature_keys=("mean_return",), target_key="duration"
        )

    def test_chronological_split_keeps_newest_five_as_holdout(self):
        result = self.prepare(self.make_samples())
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["status"], "ready_for_shadow_training")
        self.assertEqual(result["training_samples"], DEFAULT_MIN_TRAINING_SAMPLES)
        self.assertEqual(result["holdout_samples"], DEFAULT_HOLDOUT_SAMPLES)
        self.assertEqual(result["training"][0]["timestamp"], 1)
        self.assertEqual(result["training"][-1]["timestamp"], 20)
        self.assertEqual(result["holdout"][0]["timestamp"], 21)
        self.assertEqual(result["holdout"][-1]["timestamp"], 25)

    def test_insufficient_data_never_exposes_partial_training_split(self):
        result = self.prepare(self.make_samples(24))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["training"], [])
        self.assertEqual(result["holdout"], [])

    def test_incomplete_and_invalid_rows_are_rejected_without_imputation(self):
        samples = self.make_samples()
        samples[0]["complete"] = False
        samples[1]["mean_return"] = None
        result = self.prepare(samples)
        self.assertEqual(result["rejected_samples"]["incomplete"], 1)
        self.assertEqual(result["rejected_samples"]["invalid_or_missing_value"], 1)
        self.assertEqual(result["eligible_samples"], 23)
        self.assertEqual(result["status"], "collecting")

    def test_all_duplicate_timestamps_are_excluded(self):
        samples = self.make_samples()
        samples.append(dict(samples[0], duration=999))
        result = self.prepare(samples)
        self.assertEqual(result["rejected_samples"]["duplicate_timestamp"], 2)
        self.assertEqual(result["eligible_samples"], 23)
        self.assertEqual(result["status"], "collecting")

    def test_split_is_sorted_even_when_input_is_out_of_order(self):
        samples = list(reversed(self.make_samples()))
        result = self.prepare(samples)
        self.assertEqual(result["training"][0]["timestamp"], 1)
        self.assertEqual(result["holdout"][-1]["timestamp"], 25)

    def test_input_samples_are_not_mutated(self):
        samples = self.make_samples()
        before = [dict(sample) for sample in samples]
        self.prepare(samples)
        self.assertEqual(samples, before)

    def test_rejects_target_or_timestamp_as_feature(self):
        with self.assertRaises(ValueError):
            prepare_chronological_dataset(
                self.make_samples(), feature_keys=("duration",),
                target_key="duration"
            )
        with self.assertRaises(ValueError):
            prepare_chronological_dataset(
                self.make_samples(), feature_keys=("timestamp",),
                target_key="duration"
            )

    def test_rejects_invalid_split_configuration(self):
        with self.assertRaises(ValueError):
            prepare_chronological_dataset(
                self.make_samples(), feature_keys=("mean_return",),
                target_key="duration", holdout_samples=0
            )


if __name__ == "__main__":
    unittest.main()
