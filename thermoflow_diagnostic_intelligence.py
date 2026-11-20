"""Read-only seven-stage ThermoFlow diagnostic pipeline (stdlib only)."""
from datetime import datetime, timezone
from statistics import median

VERSION = "1.0.0"
MAX_GAP_SECONDS = 300
RETURN_PROTECTION_C = 55.0
RETURN_WARNING_C = 54.0
MIN_TRAIN_CYCLES = 20
HOLDOUT_CYCLES = 5


def _points(raw, key):
    result = []
    for point in raw.get(key, []) or []:
        try:
            ts, value = int(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ts > 0:
            result.append((ts, value))
    return sorted(result)


def _quality(raw):
    output = {}
    for key in sorted(raw):
        points = _points(raw, key)
        gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
        output[key] = {
            "samples": len(points),
            "first": datetime.fromtimestamp(points[0][0], timezone.utc).isoformat() if points else None,
            "last": datetime.fromtimestamp(points[-1][0], timezone.utc).isoformat() if points else None,
            "gaps_over_5m": sum(gap > MAX_GAP_SECONDS for gap in gaps),
            "max_gap_seconds": max(gaps) if gaps else None,
        }
    return output


def _hydraulics(raw):
    returns = _points(raw, "thermoflow-valve4d-return")
    opening = _points(raw, "thermoflow-valve4d-opening")
    pumps = {}
    for key in ("thermoflow-pump-floor", "thermoflow-pump-dhw", "thermoflow-pump-valve4d"):
        samples, events = _points(raw, key), []
        for (old_ts, old), (ts, value) in zip(samples, samples[1:]):
            if ts - old_ts > MAX_GAP_SECONDS or old >= .5 or value < .5:
                continue
            before = next((v for t, v in reversed(returns) if t <= ts and ts - t <= 600), None)
            after = next((v for t, v in returns if ts < t <= ts + 600), None)
            if before is not None and after is not None:
                events.append({"at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                               "return_delta_c": round(after - before, 2)})
        deltas = [event["return_delta_c"] for event in events]
        pumps[key] = {"paired_starts": len(events),
                      "mean_return_delta_c": round(sum(deltas) / len(deltas), 2) if deltas else None,
                      "events": events[-30:]}
    low = [value for _, value in returns if value < RETURN_WARNING_C]
    return {
        "return_samples": len(returns),
        "return_min_c": min((value for _, value in returns), default=None),
        "return_below_54c_samples": len(low),
        "reference_return_protection_c": RETURN_PROTECTION_C,
        "warning_threshold_c": RETURN_WARNING_C,
        "valve_opening_samples": len(opening), "pump_correlations": pumps,
        "interpretation": "Korelacja czasowa, nie dowód przyczynowości. Próbki poniżej 54°C są sygnałem do przeglądu, nie potwierdzają zadziałania ochrony.",
    }


def _combustion(raw):
    flue, power = _points(raw, "thermoflow-flue"), _points(raw, "thermoflow-boiler-power")
    episodes, current, previous = [], [], None
    for ts, value in flue:
        if previous is not None and ts - previous > MAX_GAP_SECONDS and current:
            episodes.append(current)
            current = []
        if value >= 70:
            current.append((ts, value))
        elif value <= 50 and current:
            episodes.append(current)
            current = []
        previous = ts
    if current:
        episodes.append(current)
    return {
        "flue_samples": len(flue), "power_samples": len(power),
        "elevated_flue_episodes": [{
            "start": datetime.fromtimestamp(ep[0][0], timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(ep[-1][0], timezone.utc).isoformat(),
            "duration_minutes": round((ep[-1][0] - ep[0][0]) / 60, 1),
            "flue_min_c": min(v for _, v in ep), "flue_max_c": max(v for _, v in ep),
            "classification": "elevated_flue_activity", "ignition_confirmed": False,
        } for ep in episodes[-100:]],
        "fire_sensor": {"status": "separate_fuel_telemetry_audit",
                        "note": "FIRE jest dostępny w /api/fuel/telemetry-audit, nie w dwukanałowym /api/history."},
        "note": "Podwyższona temperatura spalin jest kandydatem do korelacji, nie potwierdzeniem rozpalenia.",
    }


def _cycle_stage(base):
    cycles = (base or {}).get("protection_cycles", [])
    complete = [c for c in cycles if c.get("complete") and isinstance(c.get("cold_gap_minutes"), (int, float))]
    return ({"status": "available" if cycles else "collecting",
             "source": "existing_timestamped_4d_protection_cycle_analysis",
             "cycles": cycles[-100:], "total": len(cycles), "complete": len(complete),
             "incomplete": len(cycles) - len(complete), "ml_metric": "cold_gap_minutes",
             "note": "Cykle diagnostyki zaworu 4D, nie liczba rozpaleń palnika."}, complete)


def _ml(complete):
    if len(complete) < MIN_TRAIN_CYCLES + HOLDOUT_CYCLES:
        return {"status": "collecting", "method": "robust_median_mad",
                "complete_cycles": len(complete), "required_cycles": MIN_TRAIN_CYCLES + HOLDOUT_CYCLES,
                "training_cycles": 0, "holdout_cycles": 0,
                "reason": "Za mało kompletnych cykli do walidacji chronologicznej; model nie jest trenowany."}
    train, holdout = complete[:-HOLDOUT_CYCLES], complete[-HOLDOUT_CYCLES:]
    values = [float(c["cold_gap_minutes"]) for c in train]
    center = median(values)
    mad = median([abs(v - center) for v in values])
    scale = max(1.0, mad * 1.4826)
    anomalies = []
    for cycle in holdout:
        value = float(cycle["cold_gap_minutes"])
        score = abs(value - center) / scale
        if score >= 3.5:
            anomalies.append({"protection_start": cycle.get("protection_start"),
                              "cold_gap_minutes": value, "robust_deviation": round(score, 2),
                              "severity": "review"})
    return {"status": "shadow_validated", "method": "robust_median_mad",
            "training_cycles": len(train), "holdout_cycles": len(holdout),
            "baseline_cold_gap_minutes": {"median": round(center, 2), "mad": round(mad, 2)},
            "holdout_anomalies": anomalies, "promotion": "disabled_shadow_only",
            "validation": "chronological_last_5_cycles_holdout"}


def _prediction(complete, ml):
    if ml.get("status") != "shadow_validated":
        return {"status": "unavailable",
                "reason": "Wymagane co najmniej 20 cykli treningowych i 5 chronologicznych cykli testowych."}
    values = [float(c["cold_gap_minutes"]) for c in complete[:-HOLDOUT_CYCLES]]
    center = median(values)
    mad = median([abs(v - center) for v in values])
    return {"status": "experimental", "expected_cold_gap_minutes": round(center, 1),
            "typical_range_minutes": [round(max(0, center - 2 * mad), 1), round(center + 2 * mad, 1)],
            "training_cycles": len(values), "holdout_cycles": HOLDOUT_CYCLES,
            "note": "Opisowy zakres z mediany i MAD, nie prognoza sterująca."}


def build_thermoflow_intelligence(raw, base_analysis=None, now=None):
    """Build all seven diagnostic stages; source data and control outputs stay untouched."""
    now = now or datetime.now(timezone.utc)
    quality = _quality(raw)
    core = {"status": "available" if any(v["samples"] for v in quality.values()) else "collecting",
            "version": VERSION, "timezone": str(now.tzinfo), "series": quality,
            "max_gap_seconds": MAX_GAP_SECONDS, "interpolation": False}
    cycles, complete = _cycle_stage(base_analysis)
    hydraulics, combustion = _hydraulics(raw), _combustion(raw)
    ml, prediction = _ml(complete), None
    prediction = _prediction(complete, ml)
    explanations = []
    if hydraulics["return_below_54c_samples"]:
        explanations.append({"code": "return_below_54c", "severity": "warning",
                             "samples": hydraulics["return_below_54c_samples"],
                             "message": "Wystąpiły próbki powrotu poniżej 54°C; sprawdź przebieg pomp i zaworu."})
    if cycles["incomplete"]:
        explanations.append({"code": "incomplete_4d_cycles", "severity": "info",
                             "count": cycles["incomplete"],
                             "message": "Część cykli 4D nie ma zarejestrowanego końca."})
    ai = {"status": "evidence_available" if explanations else "no_findings",
          "explanations": explanations,
          "confidence": "limited" if ml["status"] != "shadow_validated" else "moderate",
          "note": "Wyjaśnienia wynikają z jawnych reguł i dowodów; brak dowodu nie oznacza braku problemu."}
    return {"version": VERSION, "mode": "read_only", "diagnostic_core": core,
            "cycle_analyzer": cycles, "hydraulic_diagnostics": hydraulics,
            "combustion_diagnostics": combustion, "ml_baseline": ml,
            "predictive_diagnostics": prediction, "thermoflow_ai": ai,
            "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes"}
