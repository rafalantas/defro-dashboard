"""Tests for the read-only Fibaro dashboard projection."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_store import SQLiteStore
from thermoflow_fibaro_history_collector import FibaroHistoryCollector
from thermoflow_fibaro_dashboard import read_fibaro_dashboard


class FibaroDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(
            self.path,
            migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations",
        )
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store)
        self.collector.save_snapshot({
            "zones": [{
                "id": 7, "name": "Parter", "active": True,
                "properties": {
                    "currentTemperature": 20.5,
                    "currentTemperatureHeating": 19.0,
                    "currentSetpointHeating": 21.0,
                    "handSetPointHeating": 20.0,
                    "devices": [101],
                },
            }],
            "heads": [{
                "id": 101, "name": "Głowica 1", "properties": {
                    "batteryLevel": 88, "dead": False,
                    "heatRequired": True, "windowOpened": False,
                },
            }],
        }, captured_at=1000)
        self.collector.save_batch(
            [{"id": 1, "timestamp": 1100, "type": "ClimateZoneTemperatureChangedEvent",
              "data": {"id": 7, "oldValue": 20.0, "newValue": 20.5}}],
            start=1000, end=1200, collected_at=1300,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_uses_latest_snapshot_and_native_zone_events(self):
        result = read_fibaro_dashboard(self.path, days=1, now=1200)
        self.assertEqual(result["zones"][0]["name"], "Parter")
        self.assertEqual(result["zones"][0]["temperature"], 20.5)
        self.assertEqual(result["zones"][0]["setpoint"], 21.0)
        self.assertEqual(result["zones"][0]["heads"][0]["battery"], 88)
        self.assertEqual(result["history"][0]["value"], 20.5)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["safety"]["sqlite_writes"])
        self.assertFalse(result["safety"]["historical_head_zone_mapping_inferred"])

    def test_cards_read_measured_temperature_and_schedule_setpoint_like_the_real_hub(self):
        """Real zone from the installation: no currentSetpointHeating; currentTemperatureHeating is the
        active schedule target. Before the fix the card showed the target as the temperature."""
        self.collector.save_snapshot({
            "zones": [{"id": 6, "name": "Biuro", "active": True, "properties": {
                "currentTemperature": 20.6, "currentTemperatureHeating": 20.5,
                "currentSetpointHeating": None, "handSetPointHeating": 21.5, "devices": [101]}}],
            "heads": [{"id": 101, "name": "Głowica 1", "properties": {"heatRequired": False}}],
        }, captured_at=1150)
        zone = read_fibaro_dashboard(self.path, days=1, now=1200)["zones"][0]
        self.assertEqual(zone["temperature"], 20.6)
        self.assertEqual(zone["setpoint"], 20.5)
        self.assertEqual(zone["setpoint_source"], "currentTemperatureHeating")
        self.assertEqual(zone["manual_setpoint"], 21.5)     # reported, but never the active target

    def test_invalid_window_and_missing_database_fail_closed(self):
        with self.assertRaises(ValueError):
            read_fibaro_dashboard(self.path, days=15, now=1200)
        with self.assertRaises(FileNotFoundError):
            read_fibaro_dashboard(self.path.parent / "missing.sqlite3", now=1200)


if __name__ == "__main__":
    unittest.main()
