import unittest

from thermoflow_anomaly_v3 import analyze_multisignal


def samples(*, floor_starts=0, drop=0.0):
    rows = []
    state = False
    timestamp = 1_800_000_000
    for cycle in range(floor_starts):
        rows.append({
            "timestamp": timestamp, "return_c": 60.0, "boiler_c": 70.0, "flue_c": 100.0,
            "valve_opening_pct": 20.0, "pumps": {"floor": state, "dhw": False, "valve4d": True, "circulation": False},
        })
        timestamp += 30
        state = True
        rows.append({
            "timestamp": timestamp, "return_c": 60.0 + drop, "boiler_c": 69.0, "flue_c": 102.0,
            "valve_opening_pct": 24.0, "pumps": {"floor": state, "dhw": False, "valve4d": True, "circulation": False},
        })
        timestamp += 30
        state = False
    rows.append({
        "timestamp": timestamp, "return_c": 60.0, "boiler_c": 70.0, "flue_c": 100.0,
        "valve_opening_pct": 20.0, "pumps": {"floor": state, "dhw": False, "valve4d": True, "circulation": False},
    })
    return rows, timestamp


class AnomalyV3Tests(unittest.TestCase):
    def test_repeated_multisignal_pattern_requires_three_paired_events(self):
        rows, now = samples(floor_starts=3, drop=-1.5)
        result = analyze_multisignal(rows, as_of=now)
        self.assertEqual(result["schema"], "thermoflow-anomaly/3")
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["patterns"]), 1)
        pattern = result["patterns"][0]
        self.assertEqual(pattern["pump"], "floor")
        self.assertEqual(pattern["paired_events"], 3)
        self.assertEqual(pattern["median_valve_opening_delta_pct"], 4.0)
        self.assertEqual(result["causal_diagnostics"]["causality"], "not_established")

    def test_single_or_two_events_never_raise_anomaly(self):
        for count in (1, 2):
            rows, now = samples(floor_starts=count, drop=-2.0)
            result = analyze_multisignal(rows, as_of=now)
            self.assertEqual(result["patterns"], [])
            self.assertNotEqual(result["evaluated_pumps"]["floor"]["status"], "repeated_return_drop_pattern")

    def test_missing_stale_duplicate_or_discontinuous_data_fails_closed(self):
        rows, now = samples(floor_starts=3, drop=-2.0)
        cases = [
            ([], now, "missing"),
            (rows, now + 1000, "stale"),
            (rows + [dict(rows[-1])], now, "ambiguous_timestamps"),
        ]
        for values, as_of, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(analyze_multisignal(values, as_of=as_of)["data_state"], expected)
        broken = [dict(row) for index, row in enumerate(rows) if index not in (2, 3)]
        self.assertEqual(analyze_multisignal(broken, as_of=now)["data_state"], "discontinuous_history")

    def test_other_pump_transition_reduces_confidence_and_is_reported(self):
        rows, now = samples(floor_starts=3, drop=-1.5)
        for row in rows:
            if row["timestamp"] == rows[1]["timestamp"]:
                row["pumps"]["dhw"] = True
        result = analyze_multisignal(rows, as_of=now)
        self.assertEqual(result["patterns"][0]["confidence"], "low")
        self.assertEqual(result["patterns"][0]["events_with_concurrent_pump_changes"], 1)

    def test_fifteen_minute_boiler_trend_is_exposed_as_context(self):
        rows, now = samples(floor_starts=3, drop=-1.5)
        for offset in range(1, 32):
            rows.append({
                "timestamp": now + offset * 30, "return_c": 60.0, "boiler_c": 65.0,
                "flue_c": 100.0, "valve_opening_pct": 20.0,
                "pumps": {"floor": False, "dhw": False, "valve4d": True, "circulation": False},
            })
        result = analyze_multisignal(rows, as_of=rows[-1]["timestamp"])
        pattern = result["patterns"][0]
        self.assertEqual(pattern["boiler_followup_events"], 3)
        self.assertEqual(pattern["median_boiler_delta_after_15m_c"], -5.0)

    def test_invalid_sensor_values_are_missing_not_zero(self):
        rows, now = samples(floor_starts=3, drop=-1.5)
        rows[1]["return_c"] = float("nan")
        result = analyze_multisignal(rows, as_of=now)
        self.assertEqual(result["patterns"], [])


if __name__ == "__main__":
    unittest.main()
