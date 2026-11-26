"""Regression checks for the production ThermoFlow SQLite volume wiring."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ThermoFlowComposeStorageTests(unittest.TestCase):
    def test_api_uses_fixed_database_path_inside_named_volume(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            "THERMOFLOW_DB_PATH: /var/lib/thermoflow/thermoflow.sqlite3",
            compose,
        )
        self.assertIn("- thermoflow-db:/var/lib/thermoflow", compose)
        self.assertIn(
            "name: defro-dashboard-thermoflow-db",
            compose,
        )

    def test_read_only_model_mount_is_preserved_separately(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("./data/thermoflow:/run/thermoflow:ro", compose)
        self.assertIn("- thermoflow-db:/var/lib/thermoflow", compose)


if __name__ == "__main__":
    unittest.main()
