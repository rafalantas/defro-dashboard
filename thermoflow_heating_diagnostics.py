"""Curated Fibaro/eModul heating analytics and optional candidate persistence."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from thermoflow_analytics_store import read_curated_fibaro_features
from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore
from thermoflow_time_alignment import align_observations
from thermoflow_heating_aggregates import aggregate_heating_observations

AGGREGATE_DATASET = "thermoflow_heating_aggregates"
HOURLY_FEATURE_SET = "thermoflow_heating_hourly_v1"
DAILY_FEATURE_SET = "thermoflow_heating_daily_v1"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _telemetry_samples(database_path, start, end, tolerance):
    path = Path(database_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("ThermoFlow SQLite database does not exist")
    store = SQLiteStore(path)
    with store.connection() as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute(
            "SELECT source,source_key,observed_at,payload_json FROM telemetry_samples "
            "WHERE source IN ('emodul','thingspeak:fire',"
            "'thingspeak:temperatures_power','thingspeak:valve_pumps_fuel') "
            "AND observed_at BETWEEN ? AND ? ORDER BY observed_at,id",
            (max(1, start - tolerance), end + tolerance),
        ).fetchall()
    return [{"source": row["source"], "source_key": row["source_key"],
             "observed_at": int(row["observed_at"]),
             "payload_json": row["payload_json"]} for row in rows]


def _diagnostic_summary(aligned):
    observations = aligned["observations"]
    signal_counts = {}
    state_context = {True: {}, False: {}}
    seen_context = {True: set(), False: set()}
    mapping_unverified = 0
    for row in observations:
        signal = row.get("signal")
        if isinstance(signal, str):
            signal_counts[signal] = signal_counts.get(signal, 0) + 1
        if row.get("entity") == "head" and row.get("historical_zone_mapping") == "unverified":
            mapping_unverified += 1
        if signal != "heat_required" or row.get("new_value") not in (True, False):
            continue
        if not row.get("matched"):
            continue
        context = row.get("emodul")
        if not isinstance(context, dict):
            continue
        observed = context.get("observed_at", {})
        sample_key = (observed.get("unix") if isinstance(observed, dict) else None,
                      context.get("source_key"))
        state = row["new_value"]
        if sample_key in seen_context[state]:
            continue
        seen_context[state].add(sample_key)
        data = context.get("data")
        if not isinstance(data, dict):
            continue
        for path in ("temperatures.co", "temperatures.return", "temperatures.flue",
                     "temperatures.outdoor", "boiler_power_kw", "valve.current",
                     "valve.opening"):
            value = data
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket = state_context[state].setdefault(path, [])
                bucket.append(float(value))

    context_summary = {}
    for state, metrics in state_context.items():
        context_summary[str(state).lower()] = {
            key: {"count": len(values), "mean": sum(values) / len(values)}
            for key, values in sorted(metrics.items()) if values
        }
    total = len(observations)
    matched = int(aligned["counts"]["matched"])
    matched_by_source = aligned.get("counts", {}).get("matched_by_source", {})
    return {
        "schema": "thermoflow-heating-diagnostics/2",
        "status": "available" if total else "collecting",
        "event_counts_by_signal": dict(sorted(signal_counts.items())),
        "telemetry_match": {
            "matched_transitions": matched,
            "unmatched_transitions": total - matched,
            "match_rate": (matched / total) if total else None,
        },
        "head_events_with_unverified_zone_mapping": mapping_unverified,
        "telemetry_context_after_heat_required_transition": context_summary,
        "telemetry_match_by_source": dict(sorted(matched_by_source.items())),
        "interpretation": {
            "descriptive_only": True,
            "correlation_is_not_causation": True,
            "head_to_zone_mapping_inferred": False,
            "heat_required_is_transition_state_not_continuous_demand": True,
        },
    }


def persist_heating_aggregates(database_path, aggregates, *, recorded_at=None):
    """Explicitly persist compact hourly/daily candidate aggregates idempotently."""
    if not isinstance(aggregates, dict) or aggregates.get("schema") != "thermoflow-heating-aggregates/1":
        raise ValueError("unsupported heating aggregate payload")
    hourly, daily = aggregates.get("hourly"), aggregates.get("daily")
    if not isinstance(hourly, list) or not isinstance(daily, list) or not (hourly or daily):
        return {"status": "skipped", "reason": "no_observed_aggregates", "writes": 0}
    now = int(time.time()) if recorded_at is None else recorded_at
    if type(now) is not int or now <= 0:
        raise ValueError("recorded_at must be a positive Unix timestamp")
    payload = {"schema": aggregates["schema"], "timezone": aggregates["timezone"],
               "hourly": hourly, "daily": daily}
    digest = _digest(payload)
    version = "v1-" + digest[:16]
    manifest = {
        "schema": "thermoflow-heating-aggregate-dataset/1",
        "source": "fibaro_curated_v2+emodul_and_thingspeak_telemetry_samples",
        "content_sha256": digest,
        "timezone": aggregates["timezone"],
        "hourly_windows": len(hourly), "daily_windows": len(daily),
        "source_raw_events_deleted": False,
        "missing_periods_synthesized": False,
        "status": "candidate",
    }
    store = SQLiteStore(database_path)
    writes = 0
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        required = {"feature_windows", "datasets"}
        present = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(present):
            raise RuntimeError("ThermoFlow analytical schema is not ready")
        for period, feature_set, rows in (
            ("hour", HOURLY_FEATURE_SET, hourly), ("day", DAILY_FEATURE_SET, daily)
        ):
            for item in rows:
                start, end = item.get("window_start"), item.get("window_end")
                if type(start) is not int or type(end) is not int or start <= 0 or end <= start:
                    raise ValueError("aggregate row has invalid source time bounds")
                values = {"schema": "thermoflow-heating-aggregate-feature/1",
                          "period": period, "dataset_version": version, "values": item}
                encoded = _canonical(values)
                existing = db.execute(
                    "SELECT values_json FROM feature_windows WHERE feature_set=? "
                    "AND window_start=? AND window_end=? AND dataset_version=?",
                    (feature_set, start, end, version),
                ).fetchone()
                if existing is not None and existing["values_json"] != encoded:
                    raise RuntimeError("aggregate feature conflict; transaction aborted")
                cursor = db.execute(
                    "INSERT OR IGNORE INTO feature_windows(feature_set,window_start,window_end,"
                    "generated_at,dataset_version,values_json) VALUES(?,?,?,?,?,?)",
                    (feature_set, start, end, now, version, encoded),
                )
                writes += max(0, cursor.rowcount)
        manifest_json = _canonical(manifest)
        existing_dataset = db.execute(
            "SELECT content_sha256 FROM datasets WHERE name=? AND version=?",
            (AGGREGATE_DATASET, version),
        ).fetchone()
        if existing_dataset is not None and existing_dataset["content_sha256"] != digest:
            raise RuntimeError("aggregate dataset version conflict; transaction aborted")
        db.execute(
            "INSERT OR IGNORE INTO datasets(name,version,created_at,manifest_json,"
            "content_sha256,status) VALUES(?,?,?,?,?,?)",
            (AGGREGATE_DATASET, version, now, manifest_json, digest, "candidate"),
        )
        for period, feature_set, rows in (
            ("hour", HOURLY_FEATURE_SET, hourly), ("day", DAILY_FEATURE_SET, daily)
        ):
            for item in rows:
                stored = db.execute(
                    "SELECT 1 FROM feature_windows WHERE feature_set=? AND window_start=? "
                    "AND window_end=? AND dataset_version=?",
                    (feature_set, item["window_start"], item["window_end"], version),
                ).fetchone()
                if stored is None:
                    raise RuntimeError("aggregate verification failed; transaction aborted")
    return {"status": "persisted", "dataset": AGGREGATE_DATASET,
            "dataset_version": version, "dataset_status": "candidate",
            "content_sha256": digest, "inserted_feature_windows": writes,
            "raw_source_events_deleted": 0}


def read_heating_analytics(database_path=None, *, start, end,
                           tolerance_seconds=90, timezone_name="Europe/Warsaw",
                           persist=False, recorded_at=None):
    features = read_curated_fibaro_features(database_path, start=start, end=end)
    if features.get("status") != "available":
        return {"schema": "thermoflow-heating-analytics/1", "status": features.get("status"),
                "reason": features.get("reason", "no_validated_candidate_features"),
                "feature_dataset": features.get("dataset"), "aligned": None,
                "aggregates": None, "diagnostics": None,
                "persistence": {"status": "skipped", "reason": "no_candidate_features"},
                "read_only": True}
    samples = _telemetry_samples(database_path or DEFAULT_DB_PATH, start, end,
                                 tolerance_seconds)
    aligned = align_observations(features["features"], samples,
                                 tolerance_seconds=tolerance_seconds,
                                 timezone_name=timezone_name)
    aggregates = aggregate_heating_observations(aligned, timezone_name=timezone_name)
    diagnostics = _diagnostic_summary(aligned)
    persistence = {"status": "not_requested", "writes": 0}
    if persist:
        persistence = persist_heating_aggregates(database_path or DEFAULT_DB_PATH,
                                                 aggregates, recorded_at=recorded_at)
    return {
        "schema": "thermoflow-heating-analytics/1",
        "status": "available" if diagnostics["status"] == "available" else "collecting",
        "window": {"start": start, "end": end, "timezone": timezone_name},
        "feature_dataset": features["dataset"],
        "source_counts": {
            "fibaro_feature_windows": features["count"],
            "emodul_samples": sum(item.get("source") == "emodul" for item in samples),
            "thingspeak_samples": sum(str(item.get("source", "")).startswith("thingspeak:")
                                      for item in samples),
            "telemetry_samples_by_source": {
                source: sum(item.get("source") == source for item in samples)
                for source in sorted({item.get("source") for item in samples})
            },
            "fibaro_transitions": aligned["counts"]["fibaro_transitions"],
        },
        "aligned": aligned,
        "aggregates": aggregates,
        "diagnostics": diagnostics,
        "persistence": persistence,
        "read_only": not persist,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--timezone", default="Europe/Warsaw")
    parser.add_argument("--tolerance-seconds", type=int, default=90)
    parser.add_argument("--persist", action="store_true",
                        help="explicitly persist derived hourly/daily candidate aggregates")
    parser.add_argument("--recorded-at", type=int)
    args = parser.parse_args(argv)
    result = read_heating_analytics(
        args.db, start=args.start, end=args.end, timezone_name=args.timezone,
        tolerance_seconds=args.tolerance_seconds, persist=args.persist,
        recorded_at=args.recorded_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("status") in {"available", "collecting"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
