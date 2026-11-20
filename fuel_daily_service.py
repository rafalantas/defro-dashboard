"""Daily endpoint orchestration for ThermoFlow.

The compatibility facade supplies all data access and domain operations. The
legacy prefetch/lock boundaries and response contract are intentionally kept.
"""
from __future__ import annotations


def build_daily(query: dict, deps: dict) -> dict:
    try:
        days = max(1, min(int(query.get("days", ["60"])[0]), deps["max_days"]))
    except ValueError:
        raise deps["fuel_error"](400, "days musi być liczbą") from None

    # Legacy behavior: prefetch days+2 before entering the shared calculation lock.
    deps["prefetch"](days + 2)
    with deps["lock"]:
        refills = deps["refills"]()
        model = deps["fit_model"](refills)
        rows, counter = deps["daily_rows"](days)
        daily_model = deps["fit_daily_model"](rows)

    for row in rows:
        if model and row["t_mean"] is not None:
            row["model_kg"] = round(deps["predict_kg_per_day"](model, row["t_mean"]), 1)
            if row["kg_counter"] is not None:
                row["residual_kg"] = round(row["kg_counter"] - row["model_kg"], 1)

    telemetry = deps["telemetry_state"](rows)
    return {
        "model": model,
        "daily_model": daily_model,
        "counter_calibration": counter,
        "telemetry": telemetry,
        "weather_vars": {
            "solar_mj": "suma promieniowania krótkofalowego, MJ/m²",
            "wind_max_kmh": "maksymalna prędkość wiatru na 10 m, km/h",
        },
        "days": rows,
    }
