"""Read-only diagnostics that combine refill-period AI and telemetry evidence.

No values produced here are fed back into production fuel estimates or boiler control.
"""
from __future__ import annotations

from collections import Counter

from fuel_refill_ai import TELEMETRY_FEATURES

RETURN_PROTECTION_C = 55.0
RETURN_WARNING_MARGIN_C = 1.0


def build_diagnostics(rows: list[dict], ai_shadow: dict) -> dict:
    """Build four independent diagnostic views from accepted refill-period rows."""
    rows = sorted(rows, key=lambda row: (row.get("day", ""), row.get("end", "")))
    total = len(rows)
    complete = [
        row for row in rows
        if all(row.get(name) is not None for name in TELEMETRY_FEATURES)
    ]
    missing_by_feature = Counter()
    for row in rows:
        missing = row.get("telemetry_missing_features")
        if missing is None:
            missing = [name for name in TELEMETRY_FEATURES if row.get(name) is None]
        missing_by_feature.update(set(missing))

    coverage = {
        "status": "available" if total else "collecting",
        "periods_total": total,
        "periods_complete": len(complete),
        "periods_incomplete": total - len(complete),
        "complete_percent": round(100 * len(complete) / total, 1) if total else None,
        "missing_by_feature": {
            name: missing_by_feature.get(name, 0) for name in TELEMETRY_FEATURES
        },
        "note": "Kompletność dotyczy całego okresu dosypka–dosypka; braków nie interpolujemy.",
    }

    comparison = {
        "status": ai_shadow.get("status", "unavailable"),
        "method": "chronological walk-forward and fixed holdout from fuel_ai",
        "baseline": ai_shadow.get("baseline"),
        "weather_candidate": ai_shadow.get("candidate"),
        "telemetry": ai_shadow.get("telemetry", {}),
        "period_errors": ai_shadow.get("validation", {}).get("periods", []),
        "note": "Błędy są liczone na okresach testowych, których model nie używał wcześniej do treningu.",
    }

    # These are conservative screening signals from period averages, not cycle counts.
    # Actual ignition/cycle diagnosis needs timestamped raw telemetry, not daily means.
    operational_flags = []
    for row in rows:
        return_c = row.get("telemetry_return_c")
        if isinstance(return_c, (int, float)) and return_c < RETURN_PROTECTION_C - RETURN_WARNING_MARGIN_C:
            operational_flags.append({
                "period_end": row.get("end"),
                "code": "return_mean_below_protection_margin",
                "severity": "warning",
                "observed_c": round(float(return_c), 2),
                "reference_c": RETURN_PROTECTION_C,
                "message": "Średnia temperatura powrotu w okresie była poniżej 54°C; wymaga sprawdzenia przebiegu, nie oznacza automatycznie zadziałania ochrony.",
            })

    anomaly_screen = {
        "status": "screening_only",
        "flags": operational_flags,
        "flags_count": len(operational_flags),
        "cycle_detection": "requires_timestamped_raw_telemetry",
        "note": "Nie wyznaczamy liczby rozpaleń ani długości cykli ze średnich dobowych/okresowych. Nie zmieniamy nastaw ani ochrony powrotu 55°C.",
    }

    model_comparison = {
        "status": "available" if ai_shadow.get("status") == "shadow_ready" else "collecting",
        "weather_model": {
            "selected": ai_shadow.get("selected"),
            "baseline": ai_shadow.get("baseline"),
            "candidate": ai_shadow.get("candidate"),
            "mae_gain_percent": ai_shadow.get("mae_gain_percent"),
            "fixed_holdout_mae_gain_percent": ai_shadow.get("fixed_holdout_mae_gain_percent"),
        },
        "telemetry_model": ai_shadow.get("telemetry", {}),
        "promotion": "disabled_shadow_only",
        "note": "Porównanie opisowe; model telemetryczny wymaga 20 okresów treningowych i 5 testowych.",
    }

    return {
        "version": 1,
        "mode": "shadow_only",
        "telemetry_quality": coverage,
        "refill_forecast_accuracy": comparison,
        "boiler_anomaly_screen": anomaly_screen,
        "model_comparison": model_comparison,
        "safety": "diagnostics_only; production fuel estimates and boiler control are unchanged",
    }
