"""Read-only production acceptance projection; unknown evidence never passes."""
SCHEMA = "thermoflow-production-acceptance/2"
PATH = "/api/thermoflow/v1/acceptance"


def _item(name, status, detail, *, blocking=False):
    return {"name": name, "status": status, "detail": detail, "blocking": blocking}


def build_acceptance_report(*, shadow=None, live_predictions=None, intelligence=None,
                            data_quality=None, monitoring=None, digital_twin=None,
                            deployment_checks=None):
    """Build explicit gates from supplied evidence; never infer missing checks."""
    gates = []
    telemetry_status = shadow.get("status") if isinstance(shadow, dict) else None
    if telemetry_status == "screened":
        gates.append(_item("telemetry", "ready", "Fresh shadow telemetry is available."))
    elif telemetry_status == "stale":
        gates.append(_item("telemetry", "blocked", "Shadow telemetry is stale.", blocking=True))
    elif telemetry_status == "telemetry_unavailable":
        gates.append(_item("telemetry", "blocked", "Telemetry is unavailable.", blocking=True))
    else:
        gates.append(_item("telemetry", "collecting", "No complete fresh shadow assessment yet."))

    twin = digital_twin if isinstance(digital_twin, dict) else {}
    twin_state = twin.get("status")
    if twin_state == "observing":
        gates.append(_item("digital_twin", "shadow_ready",
                           "Fresh synchronized temperatures support a read-only trend projection; not a physical simulation."))
    elif twin_state == "stale":
        gates.append(_item("digital_twin", "blocked", "Digital twin input telemetry is stale.", blocking=True))
    else:
        gates.append(_item("digital_twin", "collecting", "Insufficient synchronized history for a trend projection."))

    predictions = live_predictions if isinstance(live_predictions, dict) else {}
    for key, label in (("burner_ml", "Burner ML"), ("fuel_ml", "Fuel ML")):
        entry = predictions.get(key)
        state = entry.get("status") if isinstance(entry, dict) else None
        if state == "predicted":
            status, detail = "shadow_ready", "A read-only prediction is available; manual review is still required."
        elif state == "stale":
            status, detail = "blocked", "Model input or telemetry is stale."
        elif state == "unavailable":
            status, detail = "blocked", "Model artifact is unavailable or invalid."
        else:
            status, detail = "collecting", "No current prediction; evidence or artifact is still missing."
        gates.append(_item(label, status, detail, blocking=status == "blocked"))

    for key, label in (("data_quality", "Data quality"), ("monitoring", "Model monitoring")):
        report = data_quality if key == "data_quality" else monitoring
        if not isinstance(report, dict):
            gates.append(_item(label, "not_assessed", "No report supplied; this gate cannot pass."))
            continue
        state = report.get("status")
        if state in ("blocked", "review", "invalid", "stale"):
            gates.append(_item(label, "blocked", "The supplied report contains blockers.", blocking=True))
        elif state == "ready" or state == "observing":
            gates.append(_item(label, "ready", "The supplied report has no active blocker."))
        else:
            gates.append(_item(label, "collecting", "Evidence is incomplete; this gate is not passed."))

    checks = deployment_checks if isinstance(deployment_checks, dict) else {}
    required = ("api_health", "intelligence_endpoint", "http_5xx", "frontend_smoke",
                "desktop_mobile_review", "container_rollout")
    for key in required:
        value = checks.get(key)
        if value is True:
            gates.append(_item(key, "ready", "Explicitly confirmed by the operator."))
        elif value is False:
            gates.append(_item(key, "blocked", "Operator check failed.", blocking=True))
        else:
            gates.append(_item(key, "not_assessed", "Not explicitly confirmed; not treated as success."))

    blockers = [gate["name"] for gate in gates if gate["blocking"]]
    outstanding = [gate["name"] for gate in gates if gate["status"] not in ("ready", "shadow_ready")]
    ready = not blockers and not outstanding
    return {
        "schema": SCHEMA, "path": PATH, "mode": "read_only_acceptance",
        "status": "ready_for_manual_signoff" if ready else ("blocked" if blockers else "incomplete"),
        "automatic_signoff": False, "promotion": "disabled",
        "summary": {"gates": len(gates), "passed": sum(g["status"] in ("ready", "shadow_ready") for g in gates),
                    "blockers": len(blockers), "outstanding": len(outstanding)},
        "blockers": blockers, "outstanding": outstanding, "gates": gates,
        "digital_twin": {"status": twin_state or "not_assessed",
                         "mode": twin.get("mode") if twin else None},
        "safety": {"control": "read_only_advisory", "return_protection_c": "55°C ±1°C"},
        "limitations": ["This report does not certify model quality or boiler safety.",
                       "CI success is not a substitute for production smoke tests.",
                       "Only explicitly supplied operator checks can pass deployment gates."],
    }
