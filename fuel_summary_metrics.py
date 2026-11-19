"""Pure summary payload calculations shared by ThermoFlow endpoints."""
from __future__ import annotations


def build_refills_based(recent_periods: list[dict], model: dict | None) -> dict | None:
    """Summarize the latest four heating refill periods, preserving legacy fields."""
    recent = [period for period in recent_periods if period["hdd"] >= 5][-4:]
    if not recent:
        return None
    kg = sum(period["kg"] for period in recent)
    hdd = sum(period["hdd"] for period in recent)
    days = sum(period["days"] for period in recent)
    result = {
        "periods": len(recent),
        "from": recent[0]["start"],
        "to": recent[-1]["end"],
        "kg": kg,
        "hdd": round(hdd, 1),
        "kg_per_hdd": round(kg / hdd, 2),
    }
    if model:
        result["heating_kg_per_hdd"] = round(
            (kg - model["base_kg_per_day"] * days) / hdd, 2
        )
    return result


def telemetry_state(rows: list[dict]) -> dict:
    """Build the legacy telemetry health summary from daily rows."""
    return next(
        ({"status": row["telemetry_status"], "error": row.get("telemetry_error")}
         for row in rows if row.get("telemetry_status") in ("unavailable", "partial")),
        {"status": "available", "days": sum(1 for row in rows if row.get("telemetry_samples", 0))},
    )
