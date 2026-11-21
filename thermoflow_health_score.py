"""Explainable shadow score that does not treat missing data as health."""
from thermoflow_anomaly_v2 import SCHEMA as SCREEN_SCHEMA

SCHEMA = "thermoflow-health/1"


def build_health_score(reports, *, expected, as_of):
    expected = tuple(expected)
    if not expected or len(set(expected)) != len(expected):
        raise ValueError("expected must contain distinct components")
    components = {}
    for name in expected:
        report = reports.get(name)
        if not isinstance(report, dict) or report.get("schema") != SCREEN_SCHEMA:
            components[name] = {"score": None, "reason": "missing_screen", "age_seconds": None}
            continue
        timestamp = report.get("latest_timestamp")
        age = as_of - timestamp if type(timestamp) is int and timestamp <= as_of else None
        component = {"score": None, "reason": "stale_screen", "age_seconds": age,
                     "timestamp": timestamp}
        if report.get("as_of") == as_of:
            component["reason"] = report.get("data_state", "unknown")
            state = report.get("process_state")
            if (age is not None and report.get("data_state") == "usable"
                    and isinstance(report.get("evidence"), dict) and state in
                    ("within_baseline", "review_deviation")):
                component.update(reason=state, score=100 if state == "within_baseline" else 0)
        components[name] = component
    available = [item["score"] for item in components.values() if item["score"] is not None]
    complete = len(available) == len(expected)
    return {"schema": SCHEMA, "as_of": as_of, "status": "screened" if complete else "unknown",
            "score": round(sum(available) / len(expected), 1) if complete else None,
            "coverage": {"usable": len(available), "expected": len(expected)},
            "components": components, "method": "share_of_expected_components_within_baseline",
            "interpretation": "review aid; not a reliability or safety estimate"}
