"""Regression checks for the production ThermoFlow SQLite storage wiring."""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ThermoFlowComposeStorageTests(unittest.TestCase):
    def test_docker_branding_uses_thermoflow_names_and_images(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("name: thermoflow", compose)
        self.assertIn("ghcr.io/rafalantas/thermoflow-dashboard:latest", compose)
        self.assertIn("ghcr.io/rafalantas/thermoflow-dashboard-api:latest", compose)
        self.assertIn("container_name: thermoflow-dashboard", compose)
        self.assertIn("container_name: thermoflow-dashboard-api", compose)
        self.assertIn("container_name: thermoflow-cloudflared", compose)
        self.assertNotIn("ghcr.io/rafalantas/defro-dashboard", compose)
        self.assertNotIn("container_name: defro-dashboard", compose)

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
        self.assertEqual(compose.count("source: /mnt/thermoflow/db"), 3)
        self.assertEqual(compose.count("create_host_path: false"), 3)

    def test_analytics_scheduler_runs_inside_api_without_extra_service(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        api = compose.split("  api:", 1)[1].split("  fibaro-collector:", 1)[0]
        self.assertIn("THERMOFLOW_ANALYTICS_ENABLED:", api)
        self.assertIn("THERMOFLOW_ANALYTICS_INTERVAL_SECONDS:", api)
        self.assertIn("THERMOFLOW_ANALYTICS_INITIAL_DELAY_SECONDS:", api)
        self.assertNotIn("thermoflow-analytics:", compose)
        self.assertNotIn("thermoflow_analytics_scheduler", compose)

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
