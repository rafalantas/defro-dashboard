"""Deterministic, evidence-linked diagnostic hypotheses; no device actions."""
from thermoflow_anomaly_v2 import SCHEMA as SCREEN_SCHEMA
from thermoflow_health_score import SCHEMA as HEALTH_SCHEMA

SCHEMA = "thermoflow-diagnostic-hypotheses/1"


def diagnostic_hypotheses(health, reports):
    if health.get("schema") != HEALTH_SCHEMA:
        raise ValueError("unsupported health report")
    candidates = []
    for name, component in health["components"].items():
        if component["score"] is None:
            continue
        report = reports.get(name, {})
        if (report.get("schema") != SCREEN_SCHEMA or report.get("as_of") != health["as_of"]
                or report.get("data_state") != "usable"
                or report.get("process_state") != "review_deviation"
                or component["reason"] != "review_deviation"
                or report.get("latest_timestamp") != component.get("timestamp")
                or not isinstance(report.get("evidence"), dict)):
            continue
        candidates.append({
            "id": f"baseline-deviation:{name}", "component": name,
            "status": "unconfirmed",
            "hypotheses": ["changed_operating_conditions", "sensor_or_measurement_variation"],
            "evidence_ref": f"/screens/{name.replace('~', '~0').replace('/', '~1')}",
            "timestamp": report["latest_timestamp"],
            "limitation": "the deviation does not distinguish these explanations",
        })
    status = "review" if candidates else (
        "no_signal_in_observed_components" if health["status"] == "screened" else "unknown")
    return {"schema": SCHEMA, "status": status, "as_of": health["as_of"],
            "coverage": health["coverage"], "items": candidates, "mode": "advisory_only"}
