"""Read-only monitoring for artifact freshness, shadow quality and drift."""
from datetime import datetime
from math import ceil, isfinite
import hashlib
import json
import sqlite3
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore
from thermoflow_shadow_evaluation import evaluate_shadow

SCHEMA = "thermoflow-model-monitoring/1"


def _timestamp(value):
    if type(value) is int and 0 < value <= 253402300799:
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                return int(parsed.timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def _artifact_state(name, artifact, now, max_age):
    if artifact is None:
        return {"status": "missing", "age_seconds": None, "alerts": ["artifact_missing"]}
    if not isinstance(artifact, dict):
        return {"status": "invalid", "age_seconds": None, "alerts": ["artifact_invalid"]}
    created = _timestamp(artifact.get("created_at"))
    if created is None:
        return {"status": "invalid", "age_seconds": None, "alerts": ["artifact_timestamp_invalid"]}
    if created > now:
        return {"status": "invalid", "age_seconds": now - created, "alerts": ["artifact_future_dated"]}
    age = now - created
    if age > max_age:
        return {"status": "stale", "age_seconds": age, "alerts": ["artifact_stale"]}
    return {"status": "fresh", "age_seconds": age, "alerts": []}


def monitor_models(*, now, artifacts=None, ledgers=None, artifact_errors=None,
                   max_artifact_age_seconds=30*86400, minimum_coverage=0.8,
                   regression_worsening_ratio=0.25):
    """Evaluate only recorded evidence; absence is collecting, not healthy."""
    if type(now) not in (int, float) or not isfinite(now) or now <= 0:
        raise ValueError("now must be a positive finite UTC timestamp")
    artifacts, ledgers = artifacts or {}, ledgers or {}
    artifact_errors = artifact_errors or {}
    modules = {}
    for name in ("burner_ml", "fuel_ml"):
        if name in artifact_errors:
            state = {"status": "invalid", "age_seconds": None,
                     "alerts": [artifact_errors[name]]}
        else:
            state = _artifact_state(name, artifacts.get(name), now, max_artifact_age_seconds)
        alerts = list(state["alerts"])
        evaluation = None
        ledger = ledgers.get(name)
        if ledger is None:
            alerts.append("shadow_ledger_missing")
        else:
            evaluation = evaluate_shadow(ledger)
            eligible = sum(window["eligible_cases"] for window in evaluation["windows"])
            paired = sum(window["paired_labeled"] for window in evaluation["windows"])
            coverage = paired / eligible if eligible else None
            if coverage is None:
                alerts.append("shadow_coverage_unavailable")
            elif coverage < minimum_coverage:
                alerts.append("shadow_coverage_low")
            if ledger["kind"] == "regression":
                def aggregate(side):
                    rows = [(window["paired_metrics"][side], window["paired_labeled"])
                            for window in evaluation["windows"]
                            if window["paired_metrics"][side] is not None and window["paired_labeled"]]
                    total = sum(count for _, count in rows)
                    if not total:
                        return None
                    return {
                        "mae": sum(row["mae"] * count for row, count in rows) / total,
                        "rmse": (sum(row["rmse"] ** 2 * count for row, count in rows) / total) ** 0.5,
                        "bias": sum(row["bias"] * count for row, count in rows) / total,
                        "samples": total,
                    }
                baseline, candidate = aggregate("baseline"), aggregate("candidate")
                if baseline and candidate and baseline["mae"] > 0:
                    if candidate["mae"] > baseline["mae"] * (1 + regression_worsening_ratio):
                        alerts.append("candidate_error_regression")
                elif baseline and candidate and baseline["mae"] == 0 and candidate["mae"] > 0:
                    alerts.append("candidate_error_regression")
                populated = [window["paired_metrics"]["candidate"] for window in evaluation["windows"]
                             if window["paired_metrics"]["candidate"] is not None]
                if len(populated) >= 2:
                    first_mae, last_mae = populated[0]["mae"], populated[-1]["mae"]
                    if last_mae - first_mae > max(0.25, first_mae * regression_worsening_ratio):
                        alerts.append("chronological_error_drift")
        if state["status"] in ("missing", "invalid", "stale"):
            status = state["status"]
        elif evaluation is None or evaluation["status"] != "evaluated":
            status = "collecting"
        elif any(code in alerts for code in ("candidate_error_regression", "chronological_error_drift")):
            status = "review"
        elif "shadow_coverage_low" in alerts:
            status = "limited_coverage"
        else:
            status = "observing"
        modules[name] = {"status": status, "artifact": state, "evaluation": evaluation,
                         "alerts": sorted(set(alerts))}
    overall = ("review" if any(x["status"] in ("invalid", "stale", "review") for x in modules.values())
               else "collecting" if any(x["status"] in ("missing", "collecting") for x in modules.values())
               else "limited_coverage" if any(x["status"] == "limited_coverage" for x in modules.values())
               else "observing")
    return {"schema": SCHEMA, "as_of": int(now), "mode": "read_only_shadow_monitoring",
            "promotion": "disabled", "status": overall, "modules": modules,
            "limitations": ["Drift flags are review triggers, not causal explanations.",
                            "No live installation quality can be inferred without verified shadow labels.",
                            "No automatic retraining, promotion, or controller action."]}


PATH = "/api/thermoflow/models/monitoring"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _artifact_digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _build_ledger(module_name, rows, now):
    baseline_key = ("previous_duration_minutes" if module_name == "burner_ml"
                    else "baseline_kg_per_day")
    cases = []
    for row in reversed(rows):
        try:
            entity_key = row["entity_key"]
            decision_at = int(entity_key.rsplit(":", 1)[1])
            target_at = row["target_at"]
            predicted_at = row["predicted_at"]
            if (type(decision_at) is not int or decision_at <= 1
                    or type(target_at) is not int or target_at <= decision_at
                    or target_at > now):
                continue
            prediction = json.loads(row["prediction_json"])
            features = prediction.get("features")
            candidate = prediction.get("value")
            baseline = features.get(baseline_key) if isinstance(features, dict) else None
            if (type(candidate) not in (int, float) or not isfinite(candidate)
                    or type(baseline) not in (int, float) or not isfinite(baseline)):
                continue
            actual = json.loads(row["actual_json"]) if row["actual_json"] else None
            verified = bool(
                isinstance(actual, dict)
                and (actual.get("verified") is True
                     or actual.get("complete_observed_cycle") is True)
                and type(actual.get("value")) in (int, float)
                and isfinite(actual["value"])
            )
            cases.append({
                "id": entity_key,
                "decision_at": decision_at,
                "outcome_end": target_at,
                "label": {
                    "verified": verified,
                    "available_at": now if verified else target_at,
                    "value": actual["value"] if verified else 0.0,
                },
                "baseline": {"issued_at": decision_at, "data_cutoff": decision_at - 1,
                             "value": float(baseline)},
                "candidate": {"issued_at": decision_at, "data_cutoff": decision_at - 1,
                              "value": float(candidate)},
            })
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not cases:
        return None
    cases.sort(key=lambda item: item["decision_at"])
    start = cases[0]["decision_at"]
    end = cases[-1]["decision_at"] + 1
    width = max(1, ceil((end - start) / 366))
    return {
        "schema": "thermoflow-shadow-ledger/1",
        "task": "burner-cycle-duration-minutes" if module_name == "burner_ml" else "fuel-rate-kg-per-day",
        "kind": "regression", "start": start, "end": end, "as_of": int(now),
        "window_seconds": width, "cases": cases,
    }


def monitor_sqlite_models(store, *, now):
    """Build a read-only monitoring report from registered SQLite artifacts and outcomes."""
    artifacts, ledgers, errors = {}, {}, {}
    if store is None:
        return monitor_models(now=now)
    try:
        with store.connection() as db:
            model_rows = {
                "burner_ml": db.execute(
                    "SELECT version,artifact_sha256,metadata_json FROM model_versions "
                    "WHERE model_name='burner_cycle_duration' ORDER BY created_at DESC,id DESC LIMIT 1"
                ).fetchone(),
                "fuel_ml": db.execute(
                    "SELECT version,artifact_sha256,metadata_json FROM model_versions "
                    "WHERE model_name='fuel_rate' ORDER BY created_at DESC,id DESC LIMIT 1"
                ).fetchone(),
            }
            for module_name, row in model_rows.items():
                if row is None:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"])
                    artifact = metadata.get("model_artifact") if isinstance(metadata, dict) else None
                    expected = row["artifact_sha256"]
                    if (not isinstance(artifact, dict) or not isinstance(expected, str)
                            or _artifact_digest(artifact) != expected
                            or metadata.get("model_artifact_sha256") != expected):
                        raise ValueError("artifact_checksum_mismatch")
                    artifacts[module_name] = artifact
                except (TypeError, ValueError, KeyError, RecursionError):
                    errors[module_name] = "artifact_checksum_mismatch"

                model_name = ("burner_cycle_duration" if module_name == "burner_ml" else "fuel_rate")
                prediction_rows = db.execute(
                    "SELECT entity_key,predicted_at,target_at,prediction_json,actual_json "
                    "FROM shadow_predictions WHERE model_name=? AND model_version=? "
                    "ORDER BY target_at ASC,id ASC LIMIT 5000",
                    (model_name, row["version"]),
                ).fetchall()
                ledger = _build_ledger(module_name, prediction_rows, int(now))
                if ledger is not None:
                    ledgers[module_name] = ledger
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
        return monitor_models(now=now)

    return monitor_models(now=now, artifacts=artifacts, ledgers=ledgers,
                          artifact_errors=errors)


def monitor_sqlite_database(database_path=DEFAULT_DB_PATH, *, now):
    """Open an existing database read-only through the storage adapter; never migrate/create it."""
    path = Path(database_path).expanduser()
    if not path.is_file():
        return monitor_models(now=now)
    return monitor_sqlite_models(SQLiteStore(path), now=now)
