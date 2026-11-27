"""Telemetry windows are binary-search slices; they must equal the old full-scan filters exactly.

A cold fuel summary spent more than half of its time in these windows: every ignition event and
every pump/valve transition filtered the *whole* sample series. The new code slices the sorted series.
These tests keep the old implementations as references and compare them on randomized data,
and guard against a return to scanning the whole series.
"""
import random
import unittest
from datetime import datetime, timedelta, timezone

import fuel_hydraulic_correlation as hydraulic
import fuel_telemetry_diagnostics as diagnostics

BASE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def random_series(rng, count, *, duplicates=True, step=60):
    """Sorted (ts, value) samples with irregular gaps and, optionally, repeated timestamps."""
    seconds, series = 0, []
    for _ in range(count):
        seconds += 0 if duplicates and rng.random() < 0.1 else rng.choice([step, step, step, 17, 300, 900])
        series.append((at(seconds), rng.choice([0.0, 1.0, 20.5, 55.0, 60.25, 99.0]) + rng.random() * 0.5))
    return series


# ---- the original implementations, kept verbatim as the reference ------------------------------
def legacy_window(samples, start, end):
    return [(ts, value) for ts, value in samples if start <= ts <= end]


def legacy_response(event_time, returns, valve):
    window = timedelta(minutes=hydraulic.EVENT_WINDOW_MINUTES)
    before_return = [(ts, value) for ts, value in returns if event_time - window <= ts < event_time]
    after_return = legacy_window(returns, event_time, event_time + window)
    before_valve = [(ts, value) for ts, value in valve if event_time - window <= ts < event_time]
    after_valve = legacy_window(valve, event_time, event_time + window)
    return_covered = len(before_return) >= 2 and len(after_return) >= 2
    valve_covered = len(before_valve) >= 2 and len(after_valve) >= 2
    mean, delta = hydraulic._mean, hydraulic._delta
    return {
        "window_minutes_each_side": hydraulic.EVENT_WINDOW_MINUTES,
        "return_before_samples": len(before_return), "return_after_samples": len(after_return),
        "return_mean_before_c": mean(before_return), "return_mean_after_c": mean(after_return),
        "return_mean_delta_c": delta(before_return, after_return) if return_covered else None,
        "valve_before_samples": len(before_valve), "valve_after_samples": len(after_valve),
        "valve_mean_before_pct": mean(before_valve), "valve_mean_after_pct": mean(after_valve),
        "valve_mean_delta_pct": delta(before_valve, after_valve) if valve_covered else None,
        "response_status": "observed_both_windows" if return_covered else "insufficient_window_coverage",
    }


def legacy_window_stats(normalized, start, end):
    result = {}
    for key in diagnostics.CONTEXT_KEYS:
        samples = [(ts, value) for ts, value in normalized.get(key, []) if start <= ts <= end]
        values = [value for _, value in samples]
        result[key] = diagnostics._stats(values)
        result[key]["first"] = samples[0][0].isoformat() if samples else None
        result[key]["last"] = samples[-1][0].isoformat() if samples else None
    return result


class WindowSamplesTests(unittest.TestCase):
    def test_equals_the_full_scan_on_randomized_series_including_boundaries(self):
        rng = random.Random(3)
        for count in (0, 1, 2, 5, 60, 400):
            series = random_series(rng, count)
            stamps = [ts for ts, _ in series] or [BASE]
            candidates = stamps + [at(-500), at(10 ** 6)] + [ts + timedelta(seconds=1) for ts in stamps[:20]]
            for _ in range(150):
                start, end = sorted([rng.choice(candidates), rng.choice(candidates)])
                self.assertEqual(hydraulic.window_samples(series, start, end), legacy_window(series, start, end))
                self.assertEqual(hydraulic.window_samples(series, start, end, include_end=False),
                                 [(ts, v) for ts, v in series if start <= ts < end])

    def test_inverted_or_empty_windows_are_empty_like_before(self):
        series = random_series(random.Random(1), 50)
        late, early = series[40][0], series[10][0]
        self.assertEqual(hydraulic.window_samples(series, late, early), legacy_window(series, late, early))
        self.assertEqual(hydraulic.window_samples(series, late, early), [])
        self.assertEqual(hydraulic.window_samples([], early, late), [])

    def test_duplicate_timestamps_keep_their_original_order(self):
        series = [(at(0), 1.0), (at(60), 2.0), (at(60), 3.0), (at(60), 4.0), (at(120), 5.0)]
        self.assertEqual(hydraulic.window_samples(series, at(60), at(60)), [(at(60), 2.0), (at(60), 3.0), (at(60), 4.0)])


class ResponseEquivalenceTests(unittest.TestCase):
    def test_response_equals_the_old_implementation(self):
        rng = random.Random(8)
        returns, valve = random_series(rng, 500), random_series(rng, 500, step=120)
        stamps = [ts for ts, _ in returns]
        events = [rng.choice(stamps) for _ in range(120)] + [at(-60), at(10 ** 6)] + stamps[:3]
        events += [ts + timedelta(seconds=rng.choice([-300, -1, 1, 300, 299])) for ts in stamps[:60]]
        for event in events:
            self.assertEqual(hydraulic._response(event, returns, valve), legacy_response(event, returns, valve), event)

    def test_whole_correlation_equals_a_reference_run_on_realistic_series(self):
        rng = random.Random(5)
        normalized = {"return_c": random_series(rng, 800), "valve_opening_pct": random_series(rng, 800, step=90),
                      "pump_floor": [(at(i * 60), float((i // 25) % 2)) for i in range(800)],
                      "pump_dhw": [(at(i * 60), float((i // 40) % 2)) for i in range(800)],
                      "pump_circulation": [(at(i * 60), float((i // 11) % 2)) for i in range(800)]}
        result = hydraulic.build_hydraulic_correlation(normalized)
        self.assertGreater(result["pump_transitions_count"], 10)
        for item in result["pump_transitions"] + result["valve_movements"]:
            event_time = datetime.fromisoformat(item["at"])
            self.assertEqual(item["return_response"],
                             legacy_response(event_time, normalized["return_c"], normalized["valve_opening_pct"]))

    def test_unsorted_input_is_still_handled_like_before(self):
        # Production series are always sorted; an unsorted input must not silently change results.
        ordered = [(at(i * 60), float(i % 7) * 0.5) for i in range(120)]
        shuffled = ordered[:]
        random.Random(2).shuffle(shuffled)
        normalized = {"return_c": shuffled, "valve_opening_pct": [(at(i * 60), float(i % 4) * 10) for i in range(120)],
                      "pump_floor": [(at(i * 60), float((i // 9) % 2)) for i in range(120)]}
        result = hydraulic.build_hydraulic_correlation(normalized)
        for item in result["pump_transitions"]:
            event_time = datetime.fromisoformat(item["at"])
            self.assertEqual(item["return_response"],
                             legacy_response(event_time, normalized["return_c"], normalized["valve_opening_pct"]))


class WindowStatsEquivalenceTests(unittest.TestCase):
    def test_window_stats_equals_the_old_implementation(self):
        rng = random.Random(12)
        normalized = {key: random_series(rng, 300) for key in diagnostics.CONTEXT_KEYS[:-1]}   # last key absent
        stamps = [ts for ts, _ in normalized["co_c"]]
        for _ in range(100):
            start, end = sorted([rng.choice(stamps), rng.choice(stamps)])
            start, end = start - timedelta(minutes=rng.choice([0, 5, 30])), end + timedelta(minutes=rng.choice([0, 5, 30]))
            self.assertEqual(diagnostics._window_stats(normalized, start, end),
                             legacy_window_stats(normalized, start, end))
        self.assertEqual(diagnostics._window_stats({}, at(0), at(60)), legacy_window_stats({}, at(0), at(60)))


class NoFullScanTests(unittest.TestCase):
    """The cost problem returns if windows are found by iterating the whole series again."""

    class NoScan(list):
        def __iter__(self):
            raise AssertionError("the whole series was scanned")

    def test_window_samples_does_not_iterate_the_series(self):
        series = self.NoScan(random_series(random.Random(4), 1000))
        window = hydraulic.window_samples(series, series[100][0], series[200][0])
        self.assertGreaterEqual(len(window), 100)

    def test_response_and_window_stats_do_not_iterate_the_series(self):
        rng = random.Random(6)
        returns, valve = self.NoScan(random_series(rng, 1000)), self.NoScan(random_series(rng, 1000))
        hydraulic._response(returns[500][0], returns, valve)
        normalized = {key: self.NoScan(random_series(rng, 1000)) for key in diagnostics.CONTEXT_KEYS}
        diagnostics._window_stats(normalized, returns[100][0], returns[300][0])


if __name__ == "__main__":
    unittest.main()
