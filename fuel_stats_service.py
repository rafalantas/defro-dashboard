"""Stats endpoint orchestration for ThermoFlow.

All data access and domain calculations are injected by the compatibility facade.
This keeps source precedence and runtime policy outside the HTTP orchestration.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta


def build_stats(query: dict, deps: dict) -> dict:
    try:
        days = max(1, min(int(query.get("days", ["30"])[0]), deps["max_days"]))
    except ValueError:
        raise deps["fuel_error"](400, "days musi być liczbą") from None

    today = deps["now"]().date()
    errors = []

    def prefetch(day):
        try:
            deps["prefetch_daily"](day)
        except deps["fuel_error"]:
            pass

    def safe_daily(day):
        try:
            return deps["get_daily"](day)
        except deps["fuel_error"] as error:
            return {"day": day.isoformat(), "error": error.message}

    wanted = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    # Past days are immutable once cached and the background summary worker
    # already warms them without the shared lock, so warming them here outside
    # the lock adds no new kind of concurrent access. Everything that computes
    # results (today's record, refills, model, periods, daily reads) stays
    # serialized under the shared lock exactly as restored in PR #82.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(prefetch, [day for day in wanted if day < today]))

    with deps["lock"]:
        # Preserve the legacy four-worker prefetch and lock scope for today.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(prefetch, [day for day in wanted if day >= today]))
        try:
            refills = deps["refills"]()
            model = deps["fit_model"](refills)
            periods = deps["periods"](refills)
        except deps["fuel_error"] as error:
            refills, periods, model = [], [], None
            errors.append(error.message)
        daily = [safe_daily(day) for day in wanted]

    calibration = deps["calibration"](periods)
    for row in daily:
        if row.get("burner_kwh") is None or not row.get("burner_kwh_complete", True):
            continue
        day_start = deps["day_bounds"](
            date.fromisoformat(row["day"])
        )[0]
        estimate = (
            row["burner_kwh"]
            / (deps["pellet_kwh_per_kg"] * deps["boiler_eff"])
            * deps["burner_factor"](calibration, day_start)
        )
        row["est_kg"] = round(estimate, 1)
        row["est_provisional"] = (
            day_start >= deps["counter_epoch"]
            and bool(calibration["new"].get("provisional"))
        )
        row["est_kg_per_hdd"] = (
            round(estimate / row["hdd"], 3) if row.get("hdd") else None
        )
        if model and row.get("hdd") and row["hdd"] >= 2:
            row["heating_kg_per_hdd"] = round(
                (estimate - model["base_kg_per_day"]) / row["hdd"], 2
            )

    valid = [
        row for row in daily
        if row.get("hdd") is not None and "est_kg" in row
    ]
    total_hdd = sum(row["hdd"] for row in valid)
    total_kg = sum(row["est_kg"] for row in valid)
    return {
        "model": model,
        "om_offset": deps["om_offset"](),
        "config": {
            "hdd_base": deps["active_base"](),
            "hdd_base_default": deps["hdd_base"],
            "hdd_base_fixed": deps["hdd_base_fixed"],
            "pellet_kwh_per_kg": deps["pellet_kwh_per_kg"],
            "boiler_eff": deps["boiler_eff"],
            "bag_kg": deps["bag_kg"],
        },
        "calibration": calibration,
        "counter_calibration": deps["counter_calibration"](refills) if refills else None,
        "summary": {
            "days_with_burner_data": len(valid),
            "hdd": round(total_hdd, 1),
            "est_kg": round(total_kg, 1),
            "est_kg_per_hdd": round(total_kg / total_hdd, 3) if total_hdd > 0 else None,
        },
        "refills_count": len(refills),
        "periods": periods,
        "daily": daily,
        "errors": errors,
    }
