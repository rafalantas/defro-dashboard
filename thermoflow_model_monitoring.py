"""Read-only monitoring for artifact freshness, shadow quality and drift."""
from datetime import datetime
from math import isfinite
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


def monitor_models(*, now, artifacts=None, ledgers=None, max_artifact_age_seconds=30*86400,
                   minimum_coverage=0.8, regression_worsening_ratio=0.25):
    """Evaluate only recorded evidence; absence is collecting, not healthy."""
    if type(now) not in (int, float) or not isfinite(now) or now <= 0:
        raise ValueError("now must be a positive finite UTC timestamp")
    artifacts, ledgers = artifacts or {}, ledgers or {}
    modules = {}
    for name in ("burner_ml", "fuel_ml"):
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
