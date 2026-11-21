import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import thermoflow_fuel_snapshot
from thermoflow_fuel_ml import REPLAY, evaluate_fuel_model, prepare_fuel_dataset
from thermoflow_fuel_snapshot import FIELD_COUNTER, SnapshotError, _read, build_snapshot

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(days=110)


def stamp(day=0, minutes=0):
    return START + timedelta(days=day, minutes=minutes)


def feed(entry, day, kg=None, *, minutes=0, void=None):
    return {"entry_id": entry, "created_at": stamp(day, minutes).isoformat(),
            "field1": kg, "field2": "private note", "field3": void}


def counter(start, end):
    key = f"field{FIELD_COUNTER}"
    return [{"created_at": (start + timedelta(minutes=7)).isoformat(), key: "31"},
            {"created_at": (end - timedelta(minutes=7)).isoformat(), key: "100"}]


class FuelSnapshotTests(unittest.TestCase):
    def test_import_honors_voids_merges_and_excludes_notes(self):
        rows = [feed(1, 0, 13), feed(2, 0, 15, minutes=3), feed(3, 5, 30),
                feed(4, 9, 40), feed(5, 99, void=3)]
        result = build_snapshot(rows, start=START, as_of=END, epochs=[], verified_ids=[1, 2, 4],
                                counter_reader=counter)
        first, last = result["refills"]
        self.assertEqual(first["ids"], [1, 2])
        self.assertEqual(first["kg"], 28)
        self.assertEqual(last["ids"], [4])
        self.assertEqual(result["periods"][0]["refilled_kg"], 40)
        self.assertEqual(result["periods"][0]["mass_reference"], "verified_equal_fill_level")
        self.assertNotIn("private note", json.dumps(result))

    def test_controller_reset_cannot_verify_a_fill(self):
        result = build_snapshot([feed(1, 1, 8), feed(2, 2, 10)],
                                start=START, as_of=END, epochs=[], counter_reader=counter)
        self.assertEqual(result["refills"][0]["counter"]["status"], "controller_rise")
        self.assertEqual(result["periods"][0]["mass_reference"], "unverified")
        self.assertEqual(prepare_fuel_dataset(result, allow_import_modes=True)["dataset"]["eligible_samples"], 0)

    def test_replay_is_labelled_no_future_mass_changes_earlier_baseline(self):
        rows = [feed(i + 1, i, 12 + i % 7 + (i % 3) * 0.2) for i in range(36)]
        args = dict(start=START, as_of=END, epochs=[], assume_equal_fill=True)
        result = build_snapshot(rows, **args)
        report = evaluate_fuel_model(result)
        self.assertEqual(report["status"], "evaluated")
        self.assertEqual(report["baseline_mode"], REPLAY)
        self.assertEqual(report["label_mode"], "assumed_equal_fill_level")
        self.assertIn(REPLAY, report["evaluation"])
        self.assertNotIn("as_issued_baseline", report["evaluation"])
        self.assertNotIn("baseline_issued_at", result["periods"][-1])
        with self.assertRaisesRegex(ValueError, "requires verified"):
            prepare_fuel_dataset(result)
        rows[-1]["field1"] = 90
        changed = build_snapshot(rows, **args)
        self.assertEqual(changed["periods"][:-1], result["periods"][:-1])
        self.assertEqual(changed["periods"][-1]["baseline_kg_per_day"],
                         result["periods"][-1]["baseline_kg_per_day"])

    def test_epoch_and_unverified_period_dont_supply_rates(self):
        rows = [feed(i + 1, i, i + 10) for i in range(6)]
        result = build_snapshot(rows, start=START, as_of=END, epochs=[stamp(2, 720).isoformat()],
                                verified_ids=[1, 2, 3, 4, 5, 6])
        self.assertNotIn("baseline_kg_per_day", result["periods"][3])
        self.assertEqual(result["periods"][4]["baseline_kg_per_day"], 14)
        counts = prepare_fuel_dataset(result, allow_import_modes=True)["excluded_periods_by_reason"]
        self.assertEqual(counts["crosses_calibration_epoch"], 1)

    def test_as_issued_archive_has_no_fallback_or_late_predictions(self):
        rows = [feed(i + 1, i, i + 10) for i in range(5)]
        result = build_snapshot(rows, start=START, as_of=END, epochs=[], assume_equal_fill=True,
                                archived_baselines={stamp(2): {"baseline_kg_per_day": 10,
                                                               "baseline_issued_at": stamp(3).isoformat()}})
        self.assertNotIn("baseline_kg_per_day", result["periods"][3])
        self.assertEqual(prepare_fuel_dataset(result, allow_import_modes=True)["dataset"]["eligible_samples"], 0)

    def test_cli_uses_existing_channels_and_status_does_not_print_records(self):
        calls = []

        def reader(channel, key, **kw):
            calls.append((channel, key, kw))
            return [feed(1, 0, 10), feed(2, 1, 10)] if channel == "101" else counter(*kw["bounds"])

        with (patch.multiple(thermoflow_fuel_snapshot, TS_CHANNEL_FUEL="101",
                             TS_READ_KEY_FUEL="read-refills", TS_CHANNEL_VALVE="102",
                             TS_READ_KEY_VALVE="read-counter", _read=reader),
              redirect_stdout(io.StringIO()) as stdout):
            self.assertEqual(thermoflow_fuel_snapshot.main(["--days", "365", "--status"], now=END), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["refills"], 2)
        self.assertEqual(result["counter_states"], {"controller_rise": 2})
        self.assertEqual(result["status"], "collecting")
        self.assertNotIn("read-", stdout.getvalue())
        self.assertEqual(calls[0], ("101", "read-refills", {}))
        self.assertEqual([c[0] for c in calls[1:]], ["102", "102"])
        self.assertTrue(all(c[2]["field"] == FIELD_COUNTER for c in calls[1:]))

    def test_secret_only_used_for_get_and_not_in_errors(self):
        calls = []

        def opener(url, timeout):
            calls.append((urlsplit(url), timeout))
            return io.BytesIO(b'{"feeds":[]}')

        self.assertEqual(_read("12", "private-key", opener=opener), [])
        self.assertEqual(calls[0][0].path, "/channels/12/feeds.json")
        self.assertEqual(parse_qs(calls[0][0].query)["api_key"], ["private-key"])

        def bad(url, timeout):
            raise RuntimeError(url)

        with self.assertRaisesRegex(SnapshotError, r"^ThingSpeak read failed \(RuntimeError\)$"):
            _read("12", "private-key", opener=bad)
        with self.assertRaisesRegex(SnapshotError, "truncated"):
            _read("12", "private-key", opener=lambda *_a, **_k: io.BytesIO(
                json.dumps({"feeds": [None] * 8000}).encode()))

    def test_malformed_mass_or_missing_id_refuses_the_join(self):
        for row in (feed(2, 2, "NaN"), {**feed(2, 2, 4), "entry_id": "bad"}):
            with self.assertRaisesRegex(SnapshotError, "invalid refill ledger"):
                build_snapshot([feed(1, 1, 2), row, feed(3, 3, 4)],
                               start=START, as_of=END, epochs=[])
