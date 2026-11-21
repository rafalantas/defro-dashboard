"""Deterministic, read-only synthesis of existing ThermoFlow diagnostics.

This layer prioritizes evidence already calculated by the telemetry audit. It
does not infer actuator commands, alter fuel estimates, or control equipment.
"""
from __future__ import annotations


def build_diagnostic_summary(raw_diagnostics: dict, quality: dict,
                             raw_ingestion_quality: dict | None = None) -> dict:
    raw = raw_diagnostics or {}
    quality = quality or {}
    findings = []

    hydraulics = raw.get("hydraulics") or {}
    low_episodes = hydraulics.get("low_return_episodes") or []
    if low_episodes:
        minimums = [
            item.get("return_c", {}).get("min")
            for item in low_episodes
            if isinstance(item.get("return_c"), dict)
            and item.get("return_c", {}).get("min") is not None
        ]
        findings.append({
            "code": "return_temperature_low_episode",
            "severity": "warning",
            "title": "Epizody niskiej temperatury powrotu",
            "count": len(low_episodes),
            "minimum_return_c": min(minimums) if minimums else None,
            "reference_protection_c": 55.0,
            "detection_threshold_c": hydraulics.get("warning_threshold_c", 54.0),
            "confidence": "observed_samples",
            "message": "Sygnał do analizy hydrauliki; nie potwierdza awarii ani zadziałania ochrony.",
        })

    hydraulic_correlation = raw.get("hydraulic_correlation") or {}
    pump_transitions = hydraulic_correlation.get("pump_transitions") or []
    valve_movements = hydraulic_correlation.get("valve_movements") or []
    if pump_transitions or valve_movements:
        findings.append({
            "code": "hydraulic_event_correlation",
            "severity": "info",
            "title": "Korelacja pracy pomp i zaworu 4D",
            "pump_transitions": len(pump_transitions),
            "valve_movements": len(valve_movements),
            "responses_with_sufficient_coverage": sum(
                item.get("return_response", {}).get("response_status") == "observed_both_windows"
                for item in pump_transitions + valve_movements
            ),
            "confidence": "timestamped_cooccurrence",
            "message": "Zestawienie czasowe próbek przed i po zmianie stanu pompy lub ruchu zaworu; współwystępowanie nie dowodzi przyczynowości i nie zmienia nastaw.",
        })

    cycle_hydraulic = raw.get("cycle_hydraulic_response") or {}
    if cycle_hydraulic.get("cycles_analyzed"):
        findings.append({
            "code": "ignition_hydraulic_response",
            "severity": "info",
            "title": "Hydraulika w cyklach rozpalenia",
            "cycles_analyzed": cycle_hydraulic.get("cycles_analyzed", 0),
            "cycles_with_return_coverage": cycle_hydraulic.get("cycles_with_return_coverage", 0),
            "cycles_with_pump_events": cycle_hydraulic.get("cycles_with_pump_events", 0),
            "cycles_with_valve_movements": cycle_hydraulic.get("cycles_with_valve_movements", 0),
            "confidence": "timestamped_cooccurrence",
            "message": "Zestawienie temperatury powrotu, przełączeń pomp i ruchu zaworu wokół żądań rozpalenia; współwystępowanie nie dowodzi przyczynowości.",
        })

    ignition = raw.get("ignition_activity") or {}
    cycles = ignition.get("cycles") or []
    if cycles:
        unmatched = sum(not item.get("flame_presence_observed") for item in cycles)
        slow = [
            item.get("minutes_to_flame")
            for item in cycles
            if item.get("minutes_to_flame") is not None
        ]
        findings.append({
            "code": "ignition_cycle_response",
            "severity": "info" if unmatched == 0 else "review",
            "title": "Odpowiedź cykli rozpalenia",
            "count": len(cycles),
            "without_observed_flame": unmatched,
            "median_minutes_to_flame": (
                sorted(slow)[len(slow) // 2] if slow else None
            ),
            "confidence": "correlated_telemetry",
            "message": "Żądanie wynika z przekroczenia progu histerezy; obecność płomienia pochodzi z czujnika, a nie z potwierdzenia komendy sterownika.",
        })

    firing = raw.get("firing_activity") or {}
    flue_count = firing.get("episodes_count", 0)
    fire = raw.get("fire_activity") or {}
    if flue_count and not fire.get("episodes_count"):
        findings.append({
            "code": "flue_activity_without_flame_confirmation",
            "severity": "info",
            "title": "Aktywność spalin bez potwierdzenia płomienia",
            "count": flue_count,
            "confidence": "screening_only",
            "message": "Podwyższona temperatura spalin jest kandydatem do korelacji, nie dowodem rozpalenia.",
        })

    series = quality.get("series") or {}
    stale = [
        {"signal": key, "samples": value.get("samples", 0),
         "gaps_over_5m": value.get("gaps_over_5m", 0)}
        for key, value in series.items()
        if value.get("samples", 0) == 0 or value.get("gaps_over_5m", 0) > 0
    ]
    if stale:
        findings.append({
            "code": "telemetry_coverage_gaps",
            "severity": "review",
            "title": "Luki lub brak próbek telemetrii",
            "count": len(stale),
            "signals": stale[:12],
            "confidence": "measured_coverage",
            "message": "Luki pozostają jawne; diagnostyka nie interpoluje ani nie uzupełnia próbek.",
        })

    raw_quality = raw_ingestion_quality or {}
    channels = raw_quality.get("channels") or []
    lossy = []
    for channel in channels:
        for name, field in (channel.get("fields") or {}).items():
            loss = field.get("loss_percent")
            if isinstance(loss, (int, float)) and loss >= 5:
                lossy.append({
                    "channel": channel.get("name"),
                    "signal": name,
                    "loss_percent": loss,
                    "lost_samples": field.get("lost_samples", 0),
                })
    lossy.sort(key=lambda item: item["loss_percent"], reverse=True)
    if lossy:
        findings.append({
            "code": "raw_ingestion_loss",
            "severity": "review",
            "title": "Utrata próbek na wejściu",
            "count": len(lossy),
            "signals": lossy[:5],
            "confidence": "raw_feed_audit",
            "message": "Wartości poza zakresem są raportowane diagnostycznie; nie zmieniają danych wejściowych modelu.",
        })

    if not series:
        status = "insufficient_data"
    elif any(item["severity"] in {"warning", "review"} for item in findings):
        status = "attention"
    else:
        status = "normal"

    return {
        "mode": "read_only_rule_synthesis",
        "status": status,
        "findings_count": len(findings),
        "findings": findings,
        "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
    }
