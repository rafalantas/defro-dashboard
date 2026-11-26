"""Tests for the opt-in Fibaro scheduler loop."""
import unittest
from thermoflow_fibaro_scheduler import run_scheduler


class FibaroSchedulerTests(unittest.TestCase):
    def test_runs_immediately_then_waits_between_cycles(self):
        calls = []
        waits = []
        def cycle():
            calls.append(len(calls) + 1)
            return {"status": "complete"}
        results = run_scheduler(cycle, interval_seconds=300, max_cycles=3,
                                 wait=waits.append)
        self.assertEqual(len(results), 3)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(waits, [300, 300])

    def test_cycle_error_is_sanitized_and_loop_continues(self):
        waits = []
        calls = []
        def cycle():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("sensitive upstream detail")
            return {"status": "complete"}
        results = run_scheduler(cycle, interval_seconds=60, max_cycles=2,
                                wait=waits.append)
        self.assertEqual(results[0], {"status": "error", "error_type": "RuntimeError"})
        self.assertEqual(results[1]["status"], "complete")
        self.assertEqual(waits, [60])

    def test_interval_is_bounded(self):
        with self.assertRaises(ValueError):
            run_scheduler(lambda: {}, interval_seconds=5, max_cycles=1)


if __name__ == "__main__":
    unittest.main()
