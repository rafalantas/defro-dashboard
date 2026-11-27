"""Read-only "cold zones and why" projection from the shared ThermoFlow SQLite archive.

Rule-based and advisory: no ML, no HC3 requests, no writes, no device commands.

State source
------------
Every successful ``fibaro_hc3_history`` collector run is a confirmed observation
instant: the collector fetched HC3 and compared the zone/head snapshot to the
stored one, storing it only when the content changed. The state at an
observation instant is therefore the latest stored snapshot at or before it.
Nothing is held across missing instants: a gap longer than ``MAX_SAMPLE_GAP_SECONDS``
breaks an episode and is reported as unobserved, never filled in.

Why not the HC3 event history: HC3 does not publish ``heatRequired`` or
``windowOpened`` changes there (zero events in 21 days), so demand and window
state exist only as polled snapshot state. Zone temperature events and head
setpoint events are not used for episodes in this version because the relation
between the head setpoint and the zone's active setpoint has not been verified.

Causes are hypotheses backed by listed evidence, never proof of causality.
"""
from __future__ import annotations

import time
from math import isfinite
from pathlib import Path

from thermoflow_fibaro_diagnostics import zone_reading
from thermoflow_fibaro_history_collector import COLLECTOR_NAME, FibaroHistoryCollector
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

SCHEMA = "thermoflow-fibaro-comfort/1"

COLD_GAP_K = 0.5               # active setpoint minus temperature
WARM_GAP_K = 1.0               # temperature minus active setpoint
EPISODE_MIN_SECONDS = 20 * 60  # an episode must last at least this long
SAMPLE_SECONDS = 300           # nominal collector cadence; each sample covers the next interval
MAX_SAMPLE_GAP_SECONDS = 15 * 60
STALE_SECONDS = 15 * 60
CAUSE_LOOKBACK_SECONDS = 30 * 60
SETPOINT_RISE_MIN_K = 0.3
TEMP_DROP_LOOKBACK_SECONDS = 60 * 60
TEMP_DROP_MIN_K = 0.3
VALID_TEMPERATURE = (5.0, 40.0)   # 0 °C readings are RS dropouts, not temperatures
VALID_SETPOINT = (5.0, 35.0)
MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 1, 72, 24
MAX_EPISODES_PER_ZONE = 20
EPS = 1e-9

LIMITATIONS = [
    "Przyczyny to hipotezy z wymienionymi dowodami, nie dowód przyczynowości.",
    "Stan strefy pochodzi z odczytów co ok. 5 minut; krótsze zdarzenia mogą zostać pominięte.",
    "Dane o zapotrzebowaniu i oknach istnieją tylko od początku zapisu migawek; HC3 nie zapisuje ich w historii zdarzeń.",
    "Okno jest wykrywane przez samą głowicę (brak osobnych czujników), więc wykrycie bywa opóźnione.",
    "Brak informacji o pracy kotła, zaworze i pogodzie w tej wersji: nie da się odróżnić braku mocy źródła od innych przyczyn.",
    "Luki dłuższe niż 15 minut między odczytami nie są uzupełniane; epizod jest wtedy przerywany.",
]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _ranged(value, bounds):
    number = _number(value)
    if number is None or not bounds[0] <= number <= bounds[1]:
        return None
    return number


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and str(result) == str(value).strip() else None


def _tri_any(values):
    known = [value for value in values if isinstance(value, bool)]
    if any(known):
        return True
    if known:
        return False
    return None


def parse_snapshot(payload):
    """Return {zone_id: state} for one stored HC3 snapshot."""
    if not isinstance(payload, dict):
        return {}
    heads = {}
    for head in payload.get("heads") or []:
        if isinstance(head, dict) and _positive_id(head.get("id")) is not None:
            heads[_positive_id(head["id"])] = head
    zones = {}
    for zone in payload.get("zones") or []:
        if not isinstance(zone, dict):
            continue
        zone_id = _positive_id(zone.get("id"))
        if zone_id is None:
            continue
        props = zone.get("properties") if isinstance(zone.get("properties"), dict) else {}
        raw_devices = props.get("devices")
        device_ids = sorted({
            device for item in (raw_devices if isinstance(raw_devices, list) else [])
            if (device := _positive_id(item)) is not None
        })
        head_props = [
            heads[item].get("properties") if isinstance(heads[item].get("properties"), dict) else {}
            for item in device_ids if item in heads
        ]
        # Shared rule: temperature is the measured currentTemperature only;
        # currentTemperatureHeating is the active scheduled setpoint, not a reading.
        reading = zone_reading(props)
        raw_temperature = reading["temperature"]
        zones[zone_id] = {
            "name": str(zone.get("name") or f"Strefa {zone_id}"),
            "temperature": _ranged(raw_temperature, VALID_TEMPERATURE),
            "temperature_invalid": raw_temperature is not None
            and _ranged(raw_temperature, VALID_TEMPERATURE) is None,
            "setpoint": _ranged(reading["setpoint"], VALID_SETPOINT),
            "setpoint_source": reading["setpoint_source"],
            "heat_required": _tri_any(item.get("heatRequired") for item in head_props),
            "window_open": _tri_any(item.get("windowOpened") for item in head_props),
            "heads": len(head_props),
        }
    return zones


def _state_series(snapshots, instants):
    """Pair each observation instant with the latest snapshot at or before it."""
    parsed = [(item["captured_at"], parse_snapshot(item.get("payload"))) for item in snapshots]
    series, index = [], -1
    for instant in sorted(instants):
        while index + 1 < len(parsed) and parsed[index + 1][0] <= instant:
            index += 1
        if index >= 0:
            series.append((instant, parsed[index][1]))
    return series


def _zone_samples(series):
    """{zone_id: (name, [sample, ...])} where each sample is a dict at one instant."""
    zones = {}
    for instant, state in series:
        for zone_id, zone in state.items():
            entry = zones.setdefault(zone_id, {"name": zone["name"], "samples": []})
            entry["name"] = zone["name"]
            gap = None
            if zone["temperature"] is not None and zone["setpoint"] is not None:
                gap = round(zone["setpoint"] - zone["temperature"], 2)
            entry["samples"].append({
                "at": instant, "temperature": zone["temperature"], "setpoint": zone["setpoint"],
                "setpoint_source": zone["setpoint_source"], "gap": gap, "heat_required": zone["heat_required"],
                "window_open": zone["window_open"], "invalid_temperature": zone["temperature_invalid"],
            })
    return zones


def _runs(samples, predicate):
    """Maximal runs of consecutive, observed, matching samples."""
    runs, current = [], []
    for sample in samples:
        broken = bool(current) and sample["at"] - current[-1]["at"] > MAX_SAMPLE_GAP_SECONDS
        if broken and current:
            runs.append(current)
            current = []
        if sample["gap"] is not None and predicate(sample["gap"]):
            current.append(sample)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _duration(run):
    return run[-1]["at"] - run[0]["at"] + SAMPLE_SECONDS


def _fraction(run, key):
    known = [sample[key] for sample in run if isinstance(sample[key], bool)]
    return (sum(known) / len(known)) if known else None


def _explain_cold(run, samples, window_start):
    start, end = run[0]["at"], run[-1]["at"]
    before = [s for s in samples if start - CAUSE_LOOKBACK_SECONDS <= s["at"] <= start]
    causes = []

    rises = []
    for previous, current in zip(before, before[1:]):
        if (previous["setpoint"] is not None and current["setpoint"] is not None
                and current["setpoint"] - previous["setpoint"] >= SETPOINT_RISE_MIN_K - EPS):
            rises.append((previous, current))
    if rises:
        previous, current = rises[-1]
        causes.append({
            "code": "setpoint_raised", "label": "Podniesiono nastawę",
            "confidence": "umiarkowana",
            "evidence": [f"Nastawa wzrosła z {previous['setpoint']:.1f} do {current['setpoint']:.1f} °C "
                         f"tuż przed początkiem niedoboru (harmonogram lub ręczna zmiana)."],
        })

    window_hits = [s for s in samples
                   if start - CAUSE_LOOKBACK_SECONDS <= s["at"] <= end and s["window_open"] is True]
    if window_hits:
        causes.append({
            "code": "window_open", "label": "Wykryto otwarte okno",
            "confidence": "umiarkowana",
            "evidence": [f"Głowica zgłosiła otwarte okno w {len(window_hits)} odczytach w okolicy epizodu."],
        })

    demand = _fraction(run, "heat_required")
    if demand is not None and demand <= 0.2:
        causes.append({
            "code": "no_demand", "label": "Głowica nie zgłasza zapotrzebowania na ciepło",
            "confidence": "niska",
            "evidence": [f"Zapotrzebowanie zgłaszane w {round(demand * 100)}% odczytów mimo niedoboru "
                         f"(histereza strefy lub własny czujnik głowicy)."],
        })
    elif demand is not None and demand >= 0.5:
        causes.append({
            "code": "demand_unsatisfied", "label": "Zapotrzebowanie zgłoszone, strefa nie dogrzewa się",
            "confidence": "niska",
            "evidence": [f"Zapotrzebowanie zgłaszane w {round(demand * 100)}% odczytów, a niedobór trwa: "
                         f"sprawdź źródło ciepła i hydraulikę (dane o kotle nie są w tej wersji uwzględnione)."],
        })

    lookback = [s for s in samples
                if start - TEMP_DROP_LOOKBACK_SECONDS <= s["at"] <= start and s["temperature"] is not None]
    if lookback and run[0]["temperature"] is not None:
        drop = max(s["temperature"] for s in lookback) - run[0]["temperature"]
        if drop >= TEMP_DROP_MIN_K - EPS and not any(c["code"] == "setpoint_raised" for c in causes):
            causes.append({
                "code": "temperature_drop", "label": "Spadek temperatury w strefie",
                "confidence": "niska",
                "evidence": [f"Temperatura spadła o {drop:.1f} K w ciągu godziny przed epizodem."],
            })

    if not causes:
        causes.append({
            "code": "unknown", "label": "Brak danych wskazujących przyczynę",
            "confidence": "brak", "evidence": [],
        })
    return causes


def _episode(run, samples, window_start, latest_at, now, kind):
    first, last = run[0], run[-1]
    ongoing = last["at"] == latest_at and now - last["at"] <= STALE_SECONDS
    result = {
        "start": first["at"], "end": last["at"] + SAMPLE_SECONDS,
        "duration_minutes": round(_duration(run) / 60),
        "ongoing": ongoing,
        "started_before_window": first["at"] - window_start <= SAMPLE_SECONDS,
        "worst_gap_k": round(max(s["gap"] for s in run), 2) if kind == "cold"
        else round(max(-s["gap"] for s in run), 2),
        "min_temperature": min(s["temperature"] for s in run),
        "setpoint": last["setpoint"],
    }
    if kind == "cold":
        causes = _explain_cold(run, samples, window_start)
        result["primary_cause"] = causes[0]
        result["other_causes"] = causes[1:]
    else:
        demand = _fraction(run, "heat_required")
        result["heat_required_fraction"] = None if demand is None else round(demand, 2)
        result["note"] = ("Głowica zgłasza zapotrzebowanie mimo przegrzania."
                          if demand is not None and demand >= 0.5 else
                          "Strefa jest cieplejsza od nastawy; źródło nadmiaru ciepła nie jest rozpoznawane.")
    return result


def _status(latest, ongoing_cold, now):
    if latest is None or now - latest["at"] > STALE_SECONDS:
        return "unknown"
    if latest["gap"] is None:
        return "unknown"
    if ongoing_cold:
        return "cold"
    if latest["gap"] >= COLD_GAP_K - EPS:
        return "cooling"
    if -latest["gap"] >= WARM_GAP_K - EPS:
        return "warm"
    return "ok"


def build_comfort(snapshots, instants, *, start, now):
    series = _state_series(snapshots, [t for t in instants if start <= t <= now])
    zones_in = _zone_samples(series)
    latest_at = series[-1][0] if series else None

    zones = []
    for zone_id, entry in zones_in.items():
        samples = entry["samples"]
        latest = samples[-1] if samples else None
        cold_runs = [r for r in _runs(samples, lambda gap: gap >= COLD_GAP_K - EPS)
                     if _duration(r) >= EPISODE_MIN_SECONDS]
        warm_runs = [r for r in _runs(samples, lambda gap: -gap >= WARM_GAP_K - EPS)
                     if _duration(r) >= EPISODE_MIN_SECONDS]
        cold = [_episode(r, samples, start, latest_at, now, "cold") for r in cold_runs]
        warm = [_episode(r, samples, start, latest_at, now, "warm") for r in warm_runs]
        ongoing_cold = any(item["ongoing"] for item in cold)
        zones.append({
            "id": zone_id, "name": entry["name"],
            "status": _status(latest, ongoing_cold, now),
            "current": None if latest is None else {
                "at": latest["at"], "temperature": latest["temperature"], "setpoint": latest["setpoint"],
                "setpoint_source": latest["setpoint_source"], "gap_k": latest["gap"], "heat_required": latest["heat_required"],
                "window_open": latest["window_open"],
            },
            "cold_minutes": sum(item["duration_minutes"] for item in cold),
            "warm_minutes": sum(item["duration_minutes"] for item in warm),
            "cold_episodes": cold[-MAX_EPISODES_PER_ZONE:],
            "warm_episodes": warm[-MAX_EPISODES_PER_ZONE:],
            "invalid_temperature_samples": sum(1 for s in samples if s["invalid_temperature"]),
        })
    zones.sort(key=lambda item: (item["name"].casefold(), item["id"]))

    cause_counts = {}
    for zone in zones:
        for episode in zone["cold_episodes"]:
            code = episode["primary_cause"]["code"]
            cause_counts[code] = cause_counts.get(code, 0) + 1
    return {
        "schema": SCHEMA,
        "mode": "read_only_rules",
        "generated_at": now,
        "window": {"start": start, "end": now, "hours": round((now - start) / 3600, 1)},
        "thresholds": {
            "cold_gap_k": COLD_GAP_K, "warm_gap_k": WARM_GAP_K,
            "episode_min_minutes": EPISODE_MIN_SECONDS // 60,
            "max_sample_gap_minutes": MAX_SAMPLE_GAP_SECONDS // 60,
        },
        "coverage": {
            "samples": len(series),
            "first_sample_at": series[0][0] if series else None,
            "last_sample_at": latest_at,
            "source": COLLECTOR_NAME,
            "fresh": latest_at is not None and now - latest_at <= STALE_SECONDS,
        },
        "summary": {
            "zones": len(zones),
            "cold_now": [z["name"] for z in zones if z["status"] == "cold"],
            "cooling_now": [z["name"] for z in zones if z["status"] == "cooling"],
            "warm_now": [z["name"] for z in zones if z["status"] == "warm"],
            "unknown_now": [z["name"] for z in zones if z["status"] == "unknown"],
            "cold_episodes": sum(len(z["cold_episodes"]) for z in zones),
            "primary_causes": cause_counts,
        },
        "zones": zones,
        "limitations": LIMITATIONS,
        "safety": {"read_only": True, "hc3_requests": False, "device_commands_sent": False,
                   "heating_settings_changed": False, "database_modified": False},
    }


def read_fibaro_comfort(database_path=None, *, hours=DEFAULT_HOURS, now=None):
    if type(hours) is not int or not MIN_HOURS <= hours <= MAX_HOURS:
        raise ValueError(f"hours must be between {MIN_HOURS} and {MAX_HOURS}")
    stamp = int(time.time()) if now is None else now
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    start = max(1, stamp - hours * 3600)
    path = Path(database_path or DEFAULT_DB_PATH).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")

    collector = FibaroHistoryCollector(SQLiteStore(path))
    snapshots = collector.read_snapshots(start=start, end=stamp, limit=10000)
    with collector.store.connection() as db:
        rows = db.execute(
            "SELECT completed_at FROM collection_runs "
            "WHERE collector_name=? AND status='complete' AND completed_at IS NOT NULL "
            "AND completed_at>=? AND completed_at<=? ORDER BY completed_at",
            (COLLECTOR_NAME, start, stamp),
        ).fetchall()
    return build_comfort(snapshots, [int(row["completed_at"]) for row in rows], start=start, now=stamp)
