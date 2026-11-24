import unittest
from unittest.mock import patch

import api


class LiveStatisticsRegressionTests(unittest.TestCase):
    def setUp(self):
        api._statistics_cache.clear()

    def tearDown(self):
        api._statistics_cache.clear()

    @patch("api._translations", return_value={"550": "Temperatura podajnika"})
    @patch("api._get")
    def test_live_statistics_parses_controller_description(self, get, translations):
        get.return_value = {
            "data": {
                "history": {
                    "999": [{"x": "2026-11-24T12:00:00+01:00", "y": 42.5}]
                },
                "descriptions": {"999": "550:1"},
            }
        }

        result = api._live_statistics("test-module", "test-token")

        self.assertEqual(
            result[4]["feeder_temperature_c"],
            {
                "value": 42.5,
                "at": "2026-11-24T12:00:00+01:00",
                "label": "Temperatura podajnika",
            },
        )
        get.assert_called_once_with(
            "modules/test-module/statistics/linear/range/day", "test-token"
        )


if __name__ == "__main__":
    unittest.main()
