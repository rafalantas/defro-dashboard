import threading
import unittest

from fuel_telemetry_audit_runtime import TelemetryAuditJob


class TelemetryAuditJobTests(unittest.TestCase):
    def wait_for_finish(self, job):
        for _ in range(100):
            status = job.status()
            if status["state"] != "running":
                return status
            threading.Event().wait(0.01)
        return job.status()

    def test_audit_runs_only_after_start_and_retains_result(self):
        calls = []
        job = TelemetryAuditJob(lambda: calls.append("run") or {"total": 12})
        self.assertEqual(job.status()["state"], "never_run")
        self.assertEqual(calls, [])
        self.assertEqual(job.start()["state"], "running")
        status = self.wait_for_finish(job)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["result"], {"total": 12})
        self.assertEqual(calls, ["run"])
        self.assertEqual(job.start()["state"], "completed")
        self.assertEqual(calls, ["run"])

    def test_concurrent_start_requests_share_one_running_audit(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def audit():
            calls.append(1)
            entered.set()
            release.wait(2)
            return {"ok": True}
        job = TelemetryAuditJob(audit)
        self.assertEqual(job.start()["state"], "running")
        self.assertTrue(entered.wait(1))
        self.assertEqual(job.start()["state"], "running")
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertEqual(self.wait_for_finish(job)["state"], "completed")

    def test_failure_is_reported_without_raising_to_request_thread(self):
        job = TelemetryAuditJob(lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        self.assertEqual(job.start()["state"], "running")
        status = self.wait_for_finish(job)
        self.assertEqual(status["state"], "failed")
        self.assertIn("offline", status["error"])


if __name__ == "__main__":
    unittest.main()
