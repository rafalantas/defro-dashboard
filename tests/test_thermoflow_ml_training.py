import unittest

from thermoflow_ml_dataset import prepare_chronological_dataset
from thermoflow_ml_training import MODEL_SCHEMA_VERSION, predict, train_and_evaluate


class ThermoFlowMlTrainingTests(unittest.TestCase):
    def dataset(self, count=25):
        samples = [
            {
                "timestamp": index + 1,
                "complete": True,
                "outside_temp": float(index),
                "target": 30.0 + 2.0 * index,
            }
            for index in range(count)
        ]
        return prepare_chronological_dataset(
            samples, feature_keys=("outside_temp",), target_key="target"
        )

    def test_trains_on_oldest_twenty_and_evaluates_newest_five(self):
        result = train_and_evaluate(self.dataset())
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["model"]["training_samples"], 20)
        self.assertEqual(result["model"]["holdout_samples"], 5)
        self.assertEqual(result["model"]["model_schema_version"], MODEL_SCHEMA_VERSION)
        self.assertTrue(result["evaluation"]["holdout_is_unseen"])
        self.assertLess(result["evaluation"]["holdout"]["mae"], 1e-8)
        self.assertTrue(result["evaluation"]["beats_baseline"])

    def test_prediction_uses_versioned_shadow_artifact(self):
        result = train_and_evaluate(self.dataset())
        prediction = predict(result["model"], {"outside_temp": 30.0})
        self.assertEqual(prediction["status"], "predicted")
        self.assertAlmostEqual(prediction["prediction"], 90.0, places=7)
        self.assertEqual(prediction["mode"], "read_only_shadow")

    def test_insufficient_dataset_does_not_train(self):
        result = train_and_evaluate(self.dataset(24))
        self.assertEqual(result["status"], "collecting")
        self.assertIsNone(result["model"])

    def test_unknown_dataset_version_is_rejected(self):
        dataset = self.dataset()
        dataset["schema_version"] = "99.0.0"
        with self.assertRaises(ValueError):
            train_and_evaluate(dataset)

    def test_non_chronological_training_rows_are_rejected(self):
        dataset = self.dataset()
        dataset["training"][1]["timestamp"] = dataset["training"][0]["timestamp"]
        with self.assertRaises(ValueError):
            train_and_evaluate(dataset)

    def test_holdout_cannot_overlap_training(self):
        dataset = self.dataset()
        dataset["holdout"][0]["timestamp"] = dataset["training"][-1]["timestamp"]
        with self.assertRaises(ValueError):
            train_and_evaluate(dataset)

    def test_singular_features_are_not_silently_fit(self):
        samples = [
            {"timestamp": index + 1, "complete": True,
             "x": float(index), "x_copy": float(index), "target": float(index * 2)}
            for index in range(25)
        ]
        dataset = prepare_chronological_dataset(
            samples, feature_keys=("x", "x_copy"), target_key="target"
        )
        with self.assertRaisesRegex(ValueError, "linearly dependent"):
            train_and_evaluate(dataset)

    def test_malformed_model_cannot_predict(self):
        with self.assertRaises(ValueError):
            predict({}, {"outside_temp": 1})

    def test_missing_feature_is_not_imputed(self):
        result = train_and_evaluate(self.dataset())
        with self.assertRaises((KeyError, ValueError)):
            predict(result["model"], {})


if __name__ == "__main__":
    unittest.main()
