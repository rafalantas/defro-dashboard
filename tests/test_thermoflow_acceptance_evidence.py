import unittest
from unittest.mock import patch

from thermoflow_acceptance_evidence import build_acceptance_evidence


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_no_configured_sources_remain_unassessed(self):
        result = build_acceptance_evidence(now=1_810_000_000, environ={})
        self.assertIsNone(result["data_quality"])
        self.assertIsNone(result["monitoring"])

    def test_missing_configured_archive_is_collecting_not_green(self):
        env = {"THERMOFLOW_FIRE_SNAPSHOT_PATH": "/missing/fire.json"}
        result = build_acceptance_evidence(now=1_810_000_000, environ=env)
        self.assertEqual(result["data_quality"]["status"], "collecting")

    def test_missing_artifacts_and_ledgers_are_collecting(self):
        env = {
            "THERMOFLOW_BURNER_MODEL_PATH": "/missing/burner.json",
            "THERMOFLOW_FUEL_MODEL_PATH": "/missing/fuel.json",
            "THERMOFLOW_BURNER_LEDGER_PATH": "/missing/burner-ledger.json",
        }
        result = build_acceptance_evidence(now=1_810_000_000, environ=env)
        self.assertEqual(result["monitoring"]["status"], "collecting")
        self.assertEqual(result["monitoring"]["modules"]["burner_ml"]["status"], "missing")

    def test_invalid_configured_evidence_is_blocked_or_reviewed(self):
        with patch("thermoflow_acceptance_evidence._read_json", side_effect=ValueError("bad json")):
            result = build_acceptance_evidence(now=1_810_000_000, environ={
                "THERMOFLOW_FIRE_SNAPSHOT_PATH": "/tmp/fire.json",
                "THERMOFLOW_FUEL_SNAPSHOT_PATH": "/tmp/fuel.json",
            })
        self.assertEqual(result["data_quality"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
