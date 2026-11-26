"""Regression checks for the production ThermoFlow SQLite storage wiring."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ThermoFlowComposeStorageTests(unittest.TestCase):
    def test_api_uses_fixed_database_path_on_ssd_bind_mount(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(
            "THERMOFLOW_DB_PATH: /var/lib/thermoflow/thermoflow.sqlite3",
            compose,
        )
        self.assertIn("source: /mnt/thermoflow/db", compose)
        self.assertIn("target: /var/lib/thermoflow", compose)
        self.assertIn("create_host_path: false", compose)

    def test_api_and_collector_share_the_ssd_database_directory(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(compose.count("source: /mnt/thermoflow/db"), 2)
        self.assertEqual(compose.count("create_host_path: false"), 2)

    def test_read_only_model_mount_is_preserved_separately(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("./data/thermoflow:/run/thermoflow:ro", compose)

    def test_docker_waits_for_backing_ssd_and_thermoflow_mounts(self):
        dropin = (
            ROOT
            / "deploy"
            / "systemd"
            / "docker.service.d"
            / "thermoflow-storage.conf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "RequiresMountsFor=/mnt/timemachine/thermoflow-storage.img /mnt/thermoflow",
            dropin,
        )

    def test_cutover_runbook_installs_systemd_dependency(self):
        runbook = (
            ROOT / "docs" / "THERMOFLOW_SSD_STORAGE.md"
        ).read_text(encoding="utf-8")
        self.assertIn("systemctl daemon-reload", runbook)
        self.assertIn("docker.service.d/thermoflow-storage.conf", runbook)
        self.assertIn("Do not restart Docker", runbook)


if __name__ == "__main__":
    unittest.main()
