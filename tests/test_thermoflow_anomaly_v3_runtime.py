import unittest
from datetime import datetime, timezone

from thermoflow_shadow_runtime import ShadowRuntime


def payload(timestamp, floor, return_c):
    return {
        "connected": True,
        "fetched_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "data": {
            "fire": True,
            "temperatures": {"co": 70.0, "flue": 100.0},
            "valve": {"return": return_c, "opening": 24.0 if floor else 20.0},
            "pumps": {"floor": floor, "dhw": False, "valve4d": True, "circulation": None},
        },
    }


class AnomalyV3ShadowIntegrationTests(unittest.TestCase):
    def test_pump_regime_changes_do_not_erase_multisignal_event_history(self):
        runtime = ShadowRuntime()
        start = 1_800_000_000
        samples = [
            (False, 60.0), (True, 58.5), (False, 60.0),
            (True, 58.5), (False, 60.0), (True, 58.5),
            (False, 60.0),
        ]
        for index, (floor, returned) in enumerate(samples):
            runtime.observe(payload(start + index * 30, floor, returned))
        report = runtime.read(start + (len(samples) - 1) * 30)
        anomaly = report["anomaly_v3"]
        self.assertEqual(anomaly["status"], "available")
        self.assertEqual(len(anomaly["patterns"]), 1)
        self.assertEqual(anomaly["patterns"][0]["paired_events"], 3)
        self.assertEqual(report["causal_diagnostics"]["causality"], "not_established")


if __name__ == "__main__":
    unittest.main()
