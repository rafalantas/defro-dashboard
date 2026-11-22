import unittest
from thermoflow_production_acceptance import build_acceptance_report, SCHEMA, PATH

class ProductionAcceptanceTests(unittest.TestCase):
    def test_missing_evidence_is_not_green(self):
        result = build_acceptance_report()
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["path"], PATH)
        self.assertEqual(result["status"], "incomplete")
        self.assertGreater(result["summary"]["outstanding"], 0)
        self.assertFalse(result["automatic_signoff"])

    def test_explicit_blockers_are_reported(self):
        result = build_acceptance_report(
            shadow={"status":"stale"},
            live_predictions={"burner_ml":{"status":"stale"}, "fuel_ml":{"status":"unavailable"}},
            data_quality={"status":"blocked"}, monitoring={"status":"review"},
            deployment_checks={"api_health":False, "http_5xx":False})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("telemetry", result["blockers"])
        self.assertIn("api_health", result["blockers"])
        self.assertIn("http_5xx", result["blockers"])

    def test_full_acceptance_requires_every_operator_check(self):
        checks = {key:True for key in ("api_health","intelligence_endpoint","http_5xx",
            "frontend_smoke","desktop_mobile_review","container_rollout")}
        result = build_acceptance_report(
            shadow={"status":"screened"},
            live_predictions={"burner_ml":{"status":"predicted"}, "fuel_ml":{"status":"predicted"}},
            data_quality={"status":"ready"}, monitoring={"status":"observing"},
            digital_twin={"status":"observing","mode":"read_only_observational_projection"},
            deployment_checks=checks)
        self.assertEqual(result["status"], "ready_for_manual_signoff")
        self.assertEqual(result["summary"]["outstanding"], 0)
        self.assertFalse(result["automatic_signoff"])
        self.assertEqual(result["safety"]["return_protection_c"], "55°C ±1°C")

    def test_partial_operator_checks_do_not_pass(self):
        result = build_acceptance_report(deployment_checks={"api_health":True})
        gate = next(x for x in result["gates"] if x["name"] == "container_rollout")
        self.assertEqual(gate["status"], "not_assessed")

if __name__ == "__main__":
    unittest.main()
