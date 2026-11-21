"""Read-only process screening with an explicit telemetry quality gate."""
from collections import defaultdict
from math import isfinite
from statistics import median

SCHEMA = "thermoflow-process-screen/2"


def screen_series(points, *, as_of, max_gap=300, max_age=300, minimum=12,
                  window=48, minimum_deviation):
    """Screen one comparable-regime series, in its native units.

    Callers supply samples from the same operating regime, not mixed phases.
    Minimum deviation is in the units of the series, to suppress sensor noise.
    """
    if (type(as_of) is not int or as_of <= 0 or not 1 <= minimum <= window <= 256
            or not 0 < max_gap <= 3600 or not 0 < max_age <= 3600
            or not isfinite(minimum_deviation) or minimum_deviation <= 0):
        raise ValueError("invalid screening policy")
    buckets = defaultdict(list)
    malformed = 0
    future = 0
    for sample in points:
        if (not isinstance(sample, (tuple, list)) or len(sample) != 2
                or type(sample[0]) is not int or sample[0] <= 0):
            malformed += 1
            continue
        ts, value = sample
        if ts > as_of:
            future += 1
            continue
        if type(value) not in (int, float) or not -1e12 <= value <= 1e12:
            value = None
            malformed += 1
        buckets[ts].append(value)
    latest = max(buckets, default=None)
    age = as_of - latest if latest is not None else None
    report = {"schema": SCHEMA, "as_of": as_of, "latest_timestamp": latest,
              "age_seconds": age, "malformed_samples": malformed, "future_samples": future,
              "baseline_samples": 0, "data_state": "unavailable",
              "process_state": "unknown", "evidence": None}
    if latest is None:
        return {**report, "data_state": "missing"}
    if age > max_age:
        return {**report, "data_state": "stale"}
    if len(buckets[latest]) != 1 or buckets[latest][0] is None:
        return {**report, "data_state": "ambiguous_latest"}
    baseline = []
    next_ts = latest
    for ts in sorted(buckets, reverse=True)[1:]:
        if next_ts - ts > max_gap or len(buckets[ts]) != 1 or buckets[ts][0] is None:
            break
        baseline.append(buckets[ts][0])
        if len(baseline) == window:
            break
        next_ts = ts
    report["baseline_samples"] = len(baseline)
    if len(baseline) < minimum:
        return {**report, "data_state": "insufficient_contiguous_history"}
    center = median(baseline)
    mad = median(abs(value - center) for value in baseline)
    actual = buckets[latest][0]
    delta = actual - center
    threshold = max(minimum_deviation, 3.5 * 1.4826 * mad)
    return {**report, "data_state": "usable",
            "process_state": "review_deviation" if abs(delta) >= threshold else "within_baseline",
            "evidence": {"observed": actual, "median": center, "mad": mad,
                         "deviation": delta, "review_threshold": threshold}}
