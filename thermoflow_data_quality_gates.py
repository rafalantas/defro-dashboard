"""Fail-closed data quality gates for offline ThermoFlow ML inputs."""
from datetime import datetime
from math import isfinite

SCHEMA = "thermoflow-data-quality-report/1"
FIRE_SCHEMA = "thermoflow-raw-fire/1"
FUEL_SCHEMA = "thermoflow-verified-fuel-periods/1"


def _utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _issue(code, severity, detail):
    return {"code": code, "severity": severity, "detail": detail}


def inspect_fire_snapshot(snapshot, *, max_gap_seconds=75):
    issues, points = [], []
    if not isinstance(snapshot, dict) or snapshot.get("schema") != FIRE_SCHEMA or snapshot.get("sampling") != "raw":
        return {"status": "blocked", "issues": [_issue("invalid_fire_schema", "blocking", "Expected raw FIRE snapshot schema.")],
                "samples": 0, "complete_timeline": False}
    signals = snapshot.get("points")
    values = signals.get("thermoflow-fire") if isinstance(signals, dict) else None
    if not isinstance(values, list):
        return {"status": "blocked", "issues": [_issue("missing_fire_signal", "blocking", "Binary FIRE samples are required.")],
                "samples": 0, "complete_timeline": False}
    seen, duplicates, invalid = set(), 0, 0
    for row in values:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or type(row[0]) is not int or row[0] <= 0 or type(row[1]) not in (int, float) or row[1] not in (0, 1):
            invalid += 1
            continue
        if row[0] in seen:
            duplicates += 1
        seen.add(row[0])
        points.append((row[0], int(row[1])))
    points.sort()
    if invalid:
        issues.append(_issue("invalid_fire_samples", "blocking", f"{invalid} malformed or non-binary samples."))
    if duplicates:
        issues.append(_issue("duplicate_fire_timestamps", "blocking", f"{duplicates} duplicate timestamps."))
    gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
    large = sum(gap > max_gap_seconds for gap in gaps)
    if large:
        issues.append(_issue("fire_timeline_gaps", "blocking", f"{large} gaps exceed {max_gap_seconds} seconds."))
    transitions = sum(a[1] != b[1] for a, b in zip(points, points[1:]))
    if transitions < 2:
        issues.append(_issue("insufficient_fire_transitions", "warning", "No complete OFF → FIRE → OFF evidence yet."))
    complete = bool(points) and not invalid and not duplicates and not large and transitions >= 2
    return {"status": "ready" if complete else ("blocked" if any(x["severity"] == "blocking" for x in issues) else "collecting"),
            "issues": issues, "samples": len(points), "gaps_over_limit": large,
            "transitions": transitions, "complete_timeline": complete}


def inspect_fuel_snapshot(snapshot, *, verified_refill_ids=()):
    issues = []
    if not isinstance(snapshot, dict) or snapshot.get("schema") != FUEL_SCHEMA:
        return {"status": "blocked", "issues": [_issue("invalid_fuel_schema", "blocking", "Expected verified fuel periods schema.")],
                "periods": 0, "verified_periods": 0}
    if snapshot.get("label_mode") != "verified_equal_fill_level":
        issues.append(_issue("unverified_fill_labels", "blocking", "Assumed fill levels are not verified mass labels."))
    if snapshot.get("baseline_mode") != "as_issued":
        issues.append(_issue("missing_as_issued_baseline", "blocking", "Replay or reconstructed baselines are not historical issued forecasts."))
    ids = set(verified_refill_ids)
    refills = snapshot.get("refills", [])
    periods = snapshot.get("periods", [])
    if not isinstance(refills, list) or not isinstance(periods, list):
        issues.append(_issue("invalid_fuel_collections", "blocking", "Refills and periods must be arrays."))
        refills, periods = [], []
    refill_ids = set()
    for refill in refills:
        if not isinstance(refill, dict) or not isinstance(refill.get("ids"), list):
            issues.append(_issue("invalid_refill_record", "blocking", "Refill provenance record is malformed."))
            continue
        refill_ids.update(x for x in refill["ids"] if type(x) is int and x > 0)
    if ids and not refill_ids.issubset(ids):
        issues.append(_issue("unverified_refill_ids", "blocking", "Snapshot contains refill IDs not in the reviewed allowlist."))
    valid_periods, previous_end = 0, None
    for period in periods:
        if not isinstance(period, dict):
            issues.append(_issue("invalid_fuel_period", "blocking", "Fuel period is malformed."))
            continue
        start, end = _utc(period.get("start")), _utc(period.get("end"))
        rate, baseline, issued = period.get("refilled_kg"), period.get("baseline_kg_per_day"), _utc(period.get("baseline_issued_at"))
        if not start or not end or end <= start:
            issues.append(_issue("invalid_period_timeline", "blocking", "Fuel periods need ordered timezone-aware boundaries."))
            continue
        if previous_end is not None and start < previous_end:
            issues.append(_issue("overlapping_periods", "blocking", "Fuel periods overlap or are not chronological."))
        previous_end = end
        if type(rate) not in (int, float) or not isfinite(rate) or rate <= 0:
            issues.append(_issue("invalid_refill_mass", "blocking", "Refill mass must be positive and finite."))
            continue
        if type(baseline) not in (int, float) or not isfinite(baseline) or baseline <= 0 or not issued or issued > start:
            issues.append(_issue("invalid_as_issued_baseline", "blocking", "Baseline must be positive and issued no later than period start."))
            continue
        valid_periods += 1
    if not periods:
        issues.append(_issue("no_fuel_periods", "warning", "No eligible refill-to-refill periods found."))
    blocking = any(x["severity"] == "blocking" for x in issues)
    return {"status": "blocked" if blocking else ("ready" if valid_periods == len(periods) and periods else "collecting"),
            "issues": issues, "periods": len(periods), "verified_periods": valid_periods}


def inspect_weather_forecasts(snapshot):
    issues = []
    periods = snapshot.get("periods", []) if isinstance(snapshot, dict) else []
    forecasts = snapshot.get("forecasts", []) if isinstance(snapshot, dict) else []
    if not isinstance(periods, list) or not isinstance(forecasts, list):
        return {"status": "blocked", "issues": [_issue("invalid_weather_collections", "blocking", "Periods and forecasts must be arrays.")],
                "matched_periods": 0, "eligible_periods": 0}
    expected = {}
    for period in periods:
        if isinstance(period, dict):
            start, end = _utc(period.get("start")), _utc(period.get("end"))
            if start and end:
                expected[start] = end
    seen, matched = set(), 0
    for forecast in forecasts:
        if not isinstance(forecast, dict):
            issues.append(_issue("invalid_weather_forecast", "blocking", "Forecast record is malformed."))
            continue
        start, end = _utc(forecast.get("start")), _utc(forecast.get("end"))
        issued, available = _utc(forecast.get("issued_at")), _utc(forecast.get("available_at"))
        value = forecast.get("forecast_mean_outdoor_c")
        if start in seen:
            issues.append(_issue("duplicate_weather_forecast", "blocking", "More than one archived forecast for a period."))
            continue
        seen.add(start)
        if (start not in expected or expected[start] != end or forecast.get("kind") != "forecast"
                or not issued or not available or not issued <= available <= start
                or type(value) not in (int, float) or not isfinite(value)):
            issues.append(_issue("invalid_as_issued_weather", "blocking", "Forecast does not match the period or was not available at decision time."))
        else:
            matched += 1
    if len(expected) > matched:
        issues.append(_issue("missing_archived_weather", "warning", f"{len(expected) - matched} periods lack an eligible archived forecast."))
    blocked = any(x["severity"] == "blocking" for x in issues)
    return {"status": "blocked" if blocked else ("ready" if expected and matched == len(expected) else "collecting"),
            "issues": issues, "matched_periods": matched, "eligible_periods": len(expected)}


def build_data_quality_report(*, fire=None, fuel=None, verified_refill_ids=()):
    modules = {}
    if fire is not None:
        modules["burner_ml"] = inspect_fire_snapshot(fire)
    if fuel is not None:
        modules["fuel_ml"] = inspect_fuel_snapshot(fuel, verified_refill_ids=verified_refill_ids)
        modules["weather_impact"] = inspect_weather_forecasts(fuel)
    states = [item["status"] for item in modules.values()]
    return {"schema": SCHEMA, "mode": "offline_read_only", "promotion": "disabled",
            "status": "blocked" if "blocked" in states else ("ready" if states and all(x == "ready" for x in states) else "collecting"),
            "modules": modules}
