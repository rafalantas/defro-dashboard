import unittest

from test_thermoflow_fuel_ml import snapshot
from thermoflow_weather_impact import WEATHER, evaluate_weather_impact


def fixture():
    doc = snapshot()
    doc["forecasts"] = [
        {"start": period["start"], "end": period["end"], "kind": "forecast",
         "issued_at": period["baseline_issued_at"], "available_at": period["start"],
         WEATHER: -3 + i % 11}
        for i, period in enumerate(doc["periods"])
    ]
    return doc


class WeatherImpactTests(unittest.TestCase):
    def test_same_holdout_and_late_revision_ignored(self):
        doc = fixture()
        result = evaluate_weather_impact(doc)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["paired"], 32)
        self.assertEqual({item["samples"] for item in result["evaluation"].values()}, {5})
        late = [{**item, "available_at": item["end"], WEATHER: -85} for item in doc["forecasts"]]
        doc["forecasts"].extend(late)
        self.assertEqual(evaluate_weather_impact(doc), result)

    def test_observations_or_wrong_target_window_do_not_sneak_in(self):
        doc = fixture()
        for i, item in enumerate(doc["forecasts"]):
            if i % 2:
                item["kind"] = "observation"
            else:
                item["end"] = item["start"]
        result = evaluate_weather_impact(doc)
        self.assertEqual(result["paired"], 0)
        self.assertEqual(result["status"], "collecting")

    def test_conflicting_as_issued_values_excluded(self):
        doc = fixture()
        doc["forecasts"].append({**doc["forecasts"][5], WEATHER: 10})
        self.assertEqual(evaluate_weather_impact(doc)["paired"], 31)
