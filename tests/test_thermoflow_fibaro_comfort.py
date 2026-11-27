"""Tests for the read-only Fibaro "cold zones and why" projection."""
import tempfile
import unittest
from pathlib import Path

from thermoflow_fibaro_comfort import (
    COLD_GAP_K, EPISODE_MIN_SECONDS, build_comfort, parse_snapshot, read_fibaro_comfort,
)
from thermoflow_fibaro_diagnostics import build_fibaro_zone_diagnostics
from thermoflow_fibaro_history_collector import COLLECTOR_NAME, FibaroHistoryCollector
from thermoflow_store import SQLiteStore

STEP = 300


def snap(at, zones):
    """zones: {zone_id: (name, temp, setpoint, heat_required, window_open)} with one head each.

    Uses the real HC3 shape observed on this installation: ``currentTemperature`` is the
    measured temperature and ``currentTemperatureHeating`` is the active scheduled setpoint;
    ``currentSetpointHeating`` is absent.
    """
    return {"captured_at": at, "payload": {
        "zones": [{"id": zid, "name": name, "properties": {
            "currentTemperature": temp, "currentTemperatureHeating": sp, "devices": [zid * 100]}}
            for zid, (name, temp, sp, heat, window) in zones.items()],
        "heads": [{"id": zid * 100, "properties": {"heatRequired": heat, "windowOpened": window}}
                  for zid, (name, temp, sp, heat, window) in zones.items()],
    }}


def timeline(zone, rows, start=10_000):
    """rows: list of (temp, setpoint, heat, window) per 5-minute instant for one zone."""
    snapshots = [snap(start + i * STEP, {1: (zone, *row)}) for i, row in enumerate(rows)]
    instants = [start + i * STEP for i in range(len(rows))]
    return snapshots, instants, start, start + (len(rows) - 1) * STEP


class ParseTests(unittest.TestCase):
    def test_zero_temperature_is_invalid_not_a_reading(self):
        zones = parse_snapshot(snap(1, {1: ("Biuro", 0.0, 21.0, False, False)})["payload"])
        self.assertIsNone(zones[1]["temperature"])
        self.assertTrue(zones[1]["temperature_invalid"])

    def test_tri_state_demand_and_window(self):
        payload = {"zones": [{"id": 1, "name": "A", "properties": {
            "currentTemperatureHeating": 20.0, "currentSetpointHeating": 21.0, "devices": [10, 11]}}],
            "heads": [{"id": 10, "properties": {"heatRequired": False, "windowOpened": False}},
                      {"id": 11, "properties": {"heatRequired": True}}]}
        zone = parse_snapshot(payload)[1]
        self.assertIs(zone["heat_required"], True)
        self.assertIs(zone["window_open"], False)
        self.assertEqual(zone["heads"], 2)


class ZoneReadingTests(unittest.TestCase):
    """Regression for reading the setpoint as the temperature (seen in production: every zone
    showed its schedule target as the temperature and had no setpoint, hence 'brak danych')."""

    # Real values from the installation, zone "Biuro", 27.11.2026.
    REAL_NIGHT = {"currentTemperature": 20.3, "currentTemperatureHeating": 19.5,
                  "currentSetpointHeating": None, "handSetPointHeating": 21.5}
    REAL_DAY = {"currentTemperature": 20.6, "currentTemperatureHeating": 20.5,
                "currentSetpointHeating": None, "handSetPointHeating": 21.5}

    def parse(self, props):
        payload = {"zones": [{"id": 6, "name": "Biuro", "properties": {**props, "devices": [60]}}],
                   "heads": [{"id": 60, "properties": {"heatRequired": False}}]}
        return parse_snapshot(payload)[6]

    def test_temperature_is_the_measured_value_not_the_schedule_target(self):
        zone = self.parse(self.REAL_DAY)
        self.assertEqual(zone["temperature"], 20.6)       # was 20.5 (the target) before the fix
        self.assertEqual(zone["setpoint"], 20.5)
        self.assertEqual(zone["setpoint_source"], "currentTemperatureHeating")

    def test_manual_setpoint_is_never_used(self):
        zone = self.parse(self.REAL_NIGHT)
        self.assertEqual(zone["setpoint"], 19.5)          # not handSetPointHeating 21.5

    def test_explicit_setpoint_wins_over_the_heating_reference(self):
        zone = self.parse({"currentTemperature": 20.0, "currentTemperatureHeating": 19.0,
                           "currentSetpointHeating": 21.0})
        self.assertEqual((zone["temperature"], zone["setpoint"]), (20.0, 21.0))
        self.assertEqual(zone["setpoint_source"], "currentSetpointHeating")

    def test_missing_measured_temperature_is_unknown_not_the_target(self):
        zone = self.parse({"currentTemperatureHeating": 20.5})
        self.assertIsNone(zone["temperature"])            # never fall back to a target
        self.assertEqual(zone["setpoint"], 20.5)

    def test_agrees_with_the_full_diagnostics(self):
        for props in (self.REAL_DAY, self.REAL_NIGHT,
                      {"currentTemperature": 20.0, "currentTemperatureHeating": 19.0, "currentSetpointHeating": 21.0},
                      {"currentTemperatureHeating": 20.5}, {"currentTemperature": 21.0}):
            payload = {"zones": [{"id": 6, "name": "Biuro", "properties": {**props, "devices": [60]}}],
                       "heads": [{"id": 60, "properties": {"heatRequired": False}}]}
            diag = build_fibaro_zone_diagnostics(payload)["zones"][0]
            ours = parse_snapshot(payload)[6]
            self.assertEqual((ours["temperature"], ours["setpoint"]),
                             (diag["current_temperature_c"], diag["active_setpoint_c"]), props)

    def test_real_morning_after_the_night_setback_is_a_setpoint_raise(self):
        """27.11: target 19.5 overnight, back to 20.5 around 08:10 while the room sat at 20.0-20.3."""
        # The room stayed at 20.0 from 03:36 until 08:12 while the target went back up to 20.5.
        rows = [(20.3, 19.5, False, False)] * 4 + [(20.0, 20.5, False, False)] * 8 + [(20.3, 20.5, False, False)] * 3
        snapshots, instants, start, now = timeline("Biuro", rows)
        zone = build_comfort(snapshots, instants, start=start, now=now)["zones"][0]
        self.assertEqual(len(zone["cold_episodes"]), 1)
        cause = zone["cold_episodes"][0]["primary_cause"]
        self.assertEqual(cause["code"], "setpoint_raised")
        self.assertIn("19.5", cause["evidence"][0])
        self.assertIn("20.5", cause["evidence"][0])
        self.assertEqual(zone["current"]["setpoint_source"], "currentTemperatureHeating")


class ColdEpisodeTests(unittest.TestCase):
    def test_short_dip_is_not_an_episode(self):
        rows = [(21.0, 21.0, False, False)] * 3 + [(20.4, 21.0, True, False)] * 3 + [(21.0, 21.0, False, False)] * 3
        snapshots, instants, start, now = timeline("Salon", rows)
        result = build_comfort(snapshots, instants, start=start, now=now)
        self.assertEqual(result["zones"][0]["cold_episodes"], [])   # 15 minutes < 20

    def test_sustained_deficit_is_an_episode_with_duration(self):
        rows = [(21.0, 21.0, False, False)] * 2 + [(20.4, 21.0, True, False)] * 6 + [(21.0, 21.0, False, False)] * 2
        snapshots, instants, start, now = timeline("Salon", rows)
        zone = build_comfort(snapshots, instants, start=start, now=now)["zones"][0]
        self.assertEqual(len(zone["cold_episodes"]), 1)
        self.assertEqual(zone["cold_episodes"][0]["duration_minutes"], 30)
        self.assertFalse(zone["cold_episodes"][0]["ongoing"])
        self.assertEqual(zone["status"], "ok")

    def test_threshold_is_inclusive(self):
        rows = [(20.5, 21.0, True, False)] * 5   # gap exactly 0.5 K for 25 minutes
        snapshots, instants, start, now = timeline("Salon", rows)
        zone = build_comfort(snapshots, instants, start=start, now=now)["zones"][0]
        self.assertEqual(COLD_GAP_K, 0.5)
        self.assertEqual(len(zone["cold_episodes"]), 1)
        self.assertEqual(zone["status"], "cold")
        self.assertTrue(zone["cold_episodes"][0]["ongoing"])

    def test_gap_in_observation_breaks_episode_and_is_not_filled(self):
        rows = [(20.0, 21.0, True, False)] * 6
        snapshots, instants, start, now = timeline("Salon", rows)
        instants = instants[:3] + [t + 3600 for t in instants[3:]]   # one hour unobserved
        result = build_comfort(snapshots, instants, start=start, now=now + 3600)
        self.assertEqual(result["zones"][0]["cold_episodes"], [])    # 3 samples each side = 15 min

    def test_zero_readings_never_count_as_cold(self):
        rows = [(0.0, 21.0, True, False)] * 8
        snapshots, instants, start, now = timeline("Salon", rows)
        zone = build_comfort(snapshots, instants, start=start, now=now)["zones"][0]
        self.assertEqual(zone["cold_episodes"], [])
        self.assertEqual(zone["invalid_temperature_samples"], 8)
        self.assertEqual(zone["status"], "unknown")

    def test_stale_data_is_unknown_not_ok(self):
        rows = [(21.0, 21.0, False, False)] * 4
        snapshots, instants, start, last = timeline("Salon", rows)
        result = build_comfort(snapshots, instants, start=start, now=last + 3600)
        self.assertEqual(result["zones"][0]["status"], "unknown")
        self.assertFalse(result["coverage"]["fresh"])


class CauseTests(unittest.TestCase):
    def cause(self, rows):
        snapshots, instants, start, now = timeline("Salon", rows)
        episode = build_comfort(snapshots, instants, start=start, now=now)["zones"][0]["cold_episodes"][0]
        return episode["primary_cause"], episode["other_causes"]

    def test_setpoint_raise_before_onset(self):
        rows = [(20.5, 20.0, False, False)] * 3 + [(20.5, 21.0, True, False)] * 6
        primary, _ = self.cause(rows)
        self.assertEqual(primary["code"], "setpoint_raised")
        self.assertIn("20.0", primary["evidence"][0])
        self.assertIn("21.0", primary["evidence"][0])

    def test_window_open_is_reported(self):
        rows = [(21.0, 21.0, False, False)] * 2 + [(20.3, 21.0, True, True)] * 3 + [(20.2, 21.0, True, False)] * 3
        primary, _ = self.cause(rows)
        self.assertEqual(primary["code"], "window_open")

    def test_no_demand_despite_deficit(self):
        rows = [(20.4, 21.0, False, False)] * 6
        primary, _ = self.cause(rows)
        self.assertEqual(primary["code"], "no_demand")
        self.assertEqual(primary["confidence"], "niska")

    def test_demand_unsatisfied(self):
        rows = [(20.4, 21.0, True, False)] * 6
        primary, _ = self.cause(rows)
        self.assertEqual(primary["code"], "demand_unsatisfied")

    def test_unknown_demand_gives_no_invented_cause(self):
        rows = [(20.4, 21.0, None, None)] * 6
        primary, other = self.cause(rows)
        self.assertEqual(primary["code"], "unknown")
        self.assertEqual(primary["confidence"], "brak")
        self.assertEqual(other, [])


class WarmAndSummaryTests(unittest.TestCase):
    def test_overheating_zone_is_reported_with_demand_note(self):
        rows = [(23.4, 21.5, True, False)] * 6
        snapshots, instants, start, now = timeline("Łazienka", rows)
        result = build_comfort(snapshots, instants, start=start, now=now)
        zone = result["zones"][0]
        self.assertEqual(zone["status"], "warm")
        self.assertEqual(len(zone["warm_episodes"]), 1)
        self.assertIn("mimo przegrzania", zone["warm_episodes"][0]["note"])
        self.assertEqual(result["summary"]["warm_now"], ["Łazienka"])

    def test_summary_lists_cold_zones_and_cause_counts(self):
        start = 10_000
        snapshots, instants = [], []
        for i in range(6):
            at = start + i * STEP
            snapshots.append(snap(at, {1: ("Biuro", 20.0, 21.0, True, False),
                                       2: ("Salon", 21.0, 21.0, False, False)}))
            instants.append(at)
        result = build_comfort(snapshots, instants, start=start, now=start + 5 * STEP)
        self.assertEqual(result["summary"]["cold_now"], ["Biuro"])
        self.assertEqual(result["summary"]["primary_causes"], {"demand_unsatisfied": 1})
        self.assertTrue(result["safety"]["read_only"])
        self.assertTrue(result["limitations"])
        self.assertGreaterEqual(EPISODE_MIN_SECONDS, 1200)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "thermoflow.sqlite3"
        self.store = SQLiteStore(self.path, migrations_dir=Path(__file__).resolve().parents[1] / "db" / "migrations")
        self.store.migrate()
        self.collector = FibaroHistoryCollector(self.store)

    def add_run(self, at, status="complete"):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO collection_runs(collector_name,started_at,completed_at,window_start,window_end,status) "
                "VALUES (?,?,?,?,?,?)", (COLLECTOR_NAME, at, at, at - 300, at, status))

    def test_reads_runs_as_observation_instants_and_ignores_failed_runs(self):
        base = 1_000_000
        self.collector.save_snapshot(snap(base, {1: ("Biuro", 20.0, 21.0, True, False)})["payload"],
                                     captured_at=base)
        for index in range(7):
            self.add_run(base + index * STEP, "complete")
        self.add_run(base + 7 * STEP, "partial")
        result = read_fibaro_comfort(self.path, hours=1, now=base + 7 * STEP)
        self.assertEqual(result["coverage"]["samples"], 7)
        zone = result["zones"][0]
        self.assertEqual(zone["name"], "Biuro")
        self.assertEqual(len(zone["cold_episodes"]), 1)
        self.assertEqual(zone["cold_episodes"][0]["primary_cause"]["code"], "demand_unsatisfied")

    def test_validates_arguments_and_missing_database(self):
        with self.assertRaises(ValueError):
            read_fibaro_comfort(self.path, hours=0)
        with self.assertRaises(ValueError):
            read_fibaro_comfort(self.path, hours=73)
        with self.assertRaises(FileNotFoundError):
            read_fibaro_comfort(Path(self.temp.name) / "missing.sqlite3")


if __name__ == "__main__":
    unittest.main()
