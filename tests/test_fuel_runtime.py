import unittest

from fuel_runtime import FuelCacheState


class FuelCacheStateTests(unittest.TestCase):
    def test_legacy_cache_shapes_and_defaults_are_preserved(self):
        state = FuelCacheState()
        self.assertEqual(state.refills, {"at": 0.0, "data": None})
        self.assertEqual(state.summary, {"at": 0.0, "data": None})
        self.assertEqual(state.summary_state, {"thread": None, "error": None})
        self.assertEqual(state.offset, {"at": 0.0, "data": None})
        self.assertEqual(state.forecast, {"at": 0.0, "data": None})
        self.assertEqual(state.climate, {"at": 0.0, "data": None})
        self.assertEqual(state.house_model, {"key": None, "data": None})
        self.assertEqual(state.daily_model, {"key": None, "data": None})
        self.assertEqual(state.telemetry_history, {"key": None, "at": 0.0, "data": None})
        self.assertEqual(state.daily, {})
        self.assertEqual(state.openmeteo, {})
        self.assertEqual(state.counter, {})
        self.assertEqual(state.weather, {})
        self.assertEqual(state.weather_fetched, {})
        self.assertEqual(state.temps_start, [])

    def test_cache_storage_is_isolated_between_engine_instances(self):
        first, second = FuelCacheState(), FuelCacheState()
        first.daily["2026-01-01"] = {"t_mean": 1}
        first.refills["data"] = []
        self.assertEqual(second.daily, {})
        self.assertIsNone(second.refills["data"])

    def test_locks_are_instance_local(self):
        first, second = FuelCacheState(), FuelCacheState()
        self.assertIsNot(first.lock, second.lock)
        self.assertIsNot(first.summary_guard, second.summary_guard)


if __name__ == "__main__":
    unittest.main()
