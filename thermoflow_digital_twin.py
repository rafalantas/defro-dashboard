"""Read-only observational thermal state and short-horizon trend projection.

This is a bounded digital-twin foundation, not a physical plant simulator:
it extrapolates recent measured trends only and never simulates actuator changes.
"""
from math import isfinite, sqrt

SCHEMA = "thermoflow-digital-twin/1"
MAX_AGE_SECONDS = 90
MAX_GAP_SECONDS = 75
MIN_SAMPLES = 8
MIN_SPAN_SECONDS = 300
HORIZONS_MINUTES = (5, 15, 30)
SIGNALS = {
    "boiler_c": ("boiler_c", -20.0, 110.0),
    "return_c": ("return_c", -20.0, 110.0),
    "flue_c": ("flue_c", -20.0, 350.0),
}


def _finite(value, low, high):
    return type(value) in (int, float) and isfinite(value) and low <= value <= high


def _fit(points):
    origin = points[0][0]
    xs = [float(ts - origin) for ts, _ in points]
    ys = [float(value) for _, value in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    rmse = sqrt(sum(error * error for error in residuals) / len(residuals))
    return slope, intercept, rmse


def build_digital_twin(samples, *, as_of):
    """Project recent contiguous measurements; missing evidence remains unavailable."""
    if type(as_of) is not int or as_of <= 0 or not isinstance(samples, (list, tuple)):
        raise ValueError("invalid digital twin input")
    rows, seen = [], set()
    for row in samples:
        if not isinstance(row, dict) or type(row.get("timestamp")) is not int:
            continue
        stamp = row["timestamp"]
        if stamp <= 0 or stamp > as_of or stamp in seen:
            continue
        seen.add(stamp)
        clean = {"timestamp": stamp}
        for key, (_, low, high) in SIGNALS.items():
            value = row.get(key)
            clean[key] = float(value) if _finite(value, low, high) else None
        pumps = row.get("pumps")
        clean["pumps"] = {
            name: value if type(value) is bool else None
            for name, value in (pumps.items() if isinstance(pumps, dict) else ())
            if name in ("floor", "dhw", "valve4d", "circulation")
        }
        rows.append(clean)
    rows.sort(key=lambda item: item["timestamp"])
    latest = rows[-1] if rows else None
    base = {
        "schema": SCHEMA, "mode": "read_only_observational_projection",
        "status": "collecting", "as_of": as_of,
        "coverage": {"samples": len(rows), "latest_timestamp": latest["timestamp"] if latest else None,
                     "age_seconds": as_of - latest["timestamp"] if latest else None,
                     "span_seconds": rows[-1]["timestamp"] - rows[0]["timestamp"] if len(rows) > 1 else 0,
                     "max_gap_seconds": None},
        "state": None, "trends": {}, "projections": {},
        "limitations": [
            "Trend extrapolation from recent measurements; not a validated physical heat-transfer model.",
            "No what-if actuator simulation, causal claim, or controller recommendation.",
            "Missing, stale, discontinuous or insufficient measurements produce no projection.",
        ],
        "safety": {"control": "read_only_advisory", "return_protection_c": "55°C ±1°C"},
    }
    if not latest:
        return base
    age = as_of - latest["timestamp"]
    if age < 0 or age > MAX_AGE_SECONDS:
        return {**base, "status": "stale"}
    if len(rows) < MIN_SAMPLES:
        return base
    gaps = [b["timestamp"] - a["timestamp"] for a, b in zip(rows, rows[1:])]
    max_gap = max(gaps) if gaps else 0
    span = rows[-1]["timestamp"] - rows[0]["timestamp"]
    base["coverage"].update(max_gap_seconds=max_gap, span_seconds=span)
    if max_gap > MAX_GAP_SECONDS or span < MIN_SPAN_SECONDS:
        return {**base, "status": "collecting"}
    state, trends, projections = {}, {}, {}
    for key in SIGNALS:
        points = [(row["timestamp"], row[key]) for row in rows if row[key] is not None]
        if len(points) < MIN_SAMPLES:
            continue
        fitted = _fit(points)
        if fitted is None:
            continue
        slope, intercept, rmse = fitted
        current = points[-1][1]
        elapsed = points[-1][0] - points[0][0]
        state[key] = round(current, 2)
        trends[key] = {"per_minute": round(slope * 60, 4), "fit_rmse_c": round(rmse, 3),
                       "samples": len(points), "span_seconds": elapsed}
        projections[key] = {}
        for minutes in HORIZONS_MINUTES:
            predicted = current + slope * minutes * 60
            if not isfinite(predicted) or predicted < SIGNALS[key][1] or predicted > SIGNALS[key][2]:
                projections[key][str(minutes)] = None
                continue
            # A conservative descriptive band, not a calibrated probability interval.
            margin = min(25.0, max(0.5, 2.0 * rmse + abs(slope) * minutes * 12 + 0.5))
            projections[key][str(minutes)] = {
                "value_c": round(predicted, 2),
                "range_low_c": round(max(SIGNALS[key][1], predicted - margin), 2),
                "range_high_c": round(min(SIGNALS[key][2], predicted + margin), 2),
                "band_type": "heuristic_not_probability",
            }
    if len(state) < 2:
        return {**base, "state": state or None, "trends": trends,
                "status": "collecting"}
    base["state"] = state
    base["trends"] = trends
    base["projections"] = projections
    base["pump_states"] = latest["pumps"]
    if state.get("boiler_c") is not None and state.get("return_c") is not None:
        base["state"]["boiler_return_delta_c"] = round(state["boiler_c"] - state["return_c"], 2)
    return {**base, "status": "observing"}
