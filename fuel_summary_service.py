"""Summary endpoint calculation orchestration for ThermoFlow.

Cache refresh, worker lifecycle and synchronization remain in the legacy facade.
This service preserves the exact calculation order and injected lock boundaries.
"""
from __future__ import annotations

from datetime import date, timedelta


def build_summary(deps: dict) -> dict:
    now = deps["now"]()
    today = now.date()
    with deps["lock"]:
        refills = deps["refills"]()
        model = deps["fit_model"](refills)
        recent_periods, calibration, counter = deps["calibration_inputs"](refills)
        last7 = [deps["get_daily"](today - timedelta(days=i)) for i in range(7, 0, -1)]
        offset = deps["om_offset"]()

    forecast = deps["forecast"]() if model else []
    known_temps = {
        date.fromisoformat(row["day"]): row["t_mean"]
        for row in last7 if row.get("t_mean") is not None
    }
    known_temps.update(dict(forecast))

    try:
        with deps["lock"]:
            daily_rows, _counter_info = deps["daily_rows"]()
            daily_model = deps["fit_daily_model"](daily_rows)
    except deps["fuel_error"]:
        daily_rows, daily_model = [], None

    try:
        # AI target comes from actual refill events and exact elapsed periods.
        # Daily counter coverage is deliberately not a prerequisite.
        ai_rows, ai_data = deps["refill_ai_rows"](refills)
        ai_shadow = deps["ai_evaluate"](ai_rows)
        ai_shadow["data_source"] = ai_data
    except Exception:
        # Experimental AI must never interrupt or alter production calculations.
        ai_shadow = {
            "status": "unavailable",
            "message": "Model eksperymentalny chwilowo niedostępny",
            "data_source": {
                "source": "refill_periods",
                "daily_refills_required": False,
            },
        }

    def estimate_burner_kg(kwh: float, moment):
        return (
            kwh / (deps["pellet_kwh_per_kg"] * deps["boiler_eff"])
            * deps["burner_factor"](calibration, moment)
        )

    def counter_kg_locked(start, end, current_counter):
        with deps["lock"]:
            return deps["counter_kg"](start, end, current_counter)

    week = deps["build_last7"](
        last7, model, calibration, counter, deps["counter_epoch"], deps["day_bounds"],
        estimate_burner_kg, counter_kg_locked, deps["predict_kg_per_day"],
    )
    refills_based = deps["build_refills_based"](recent_periods, model)

    def hopper_period_inputs(start, end, current_counter, current_calibration):
        with deps["lock"]:
            hdd_value, _kwh, _hm, kwh_missing = deps["period_sums"](start, end)
            counter_value, drop_ok, drop_floor = deps["counter_kg"](
                start, end, current_counter
            )
            burner_value = (
                deps["burner_kg"](start, end, current_calibration)
                if not kwh_missing and current_calibration["calibrated"] else None
            )
        return hdd_value, counter_value, drop_ok, drop_floor, burner_value

    last, hopper = deps["build_last_refill_and_hopper"](
        now, refills, counter, calibration, model, recent_periods, week, forecast,
        daily_model, known_temps, hopper_period_inputs, deps["counter_method"],
        deps["select_usage_estimate"], deps["days_until"], deps["hopper_kg"],
        deps["hopper_reserve_percent"],
    )
    planning = deps["planning_forecast"](model, daily_model, forecast, now, known_temps)
    telemetry = deps["telemetry_state"](daily_rows)

    forecast_rows = []
    if model:
        for day, temperature in forecast:
            weather = deps["weather"](day)
            forecast_rows.append({
                "day": day.isoformat(),
                "t_mean": temperature,
                "solar_mj": weather.get("solar_mj"),
                "wind_max_kmh": weather.get("wind_max_kmh"),
                "kg": round(
                    deps["rate_for"](
                        model, daily_model, day, temperature,
                        known_temps.get(day - timedelta(days=1)),
                    ), 1
                ),
            })

    result = {
        "configured": True,
        "telemetry": telemetry,
        "model": model,
        "hdd_base": deps["active_base"](),
        "hdd_base_fixed": deps["hdd_base_fixed"] is not None,
        "om_offset": offset,
        "daily_model": daily_model,
        "ai_shadow": ai_shadow,
        "planning": planning,
        "forecast": forecast_rows,
        "calibration": calibration,
        "counter_calibration": counter,
        "last7": week,
        "refills_based": refills_based,
        "last_refill": last,
        "hopper": hopper,
        "fetched_at": now.isoformat(),
    }
    deps["summary_cache"].update(at=deps["monotonic"](), data=result)
    return result
