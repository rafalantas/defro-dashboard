"""Robust, read-only anomaly screening for timestamped telemetry series."""
from __future__ import annotations

from statistics import median

MAX_GAP_SECONDS = 300
MIN_BASELINE_SAMPLES = 12
DEFAULT_WINDOW = 48
MAD_SCALE = 1.4826


def score_latest_sample(points, *, window: int = DEFAULT_WINDOW,
                        minimum: int = MIN_BASELINE_SAMPLES) -> dict:
    """Score the newest sample against a contiguous prior baseline using MAD.

    The latest sample is never included in its own baseline. A gap starts a new
    segment; the function does not bridge gaps or synthesize observations.
    """
    clean = []
    malformed = 0
    for point in points or []:
        try:
            ts, value = int(point[0]), float(point[1])
            if ts <= 0 or value != value or abs(value) == float("inf"):
                raise ValueError
        except (TypeError, ValueError, IndexError, OverflowError):
            malformed += 1
            continue
        clean.append((ts, value))
    clean.sort(key=lambda item: item[0])
    if not clean:
        return {"status": "collecting", "reason": "no_valid_samples", "malformed_samples": malformed}

    # Duplicate timestamps are ambiguous and excluded from the scoring stream.
    counts = {}
    for ts, _ in clean:
        counts[ts] = counts.get(ts, 0) + 1
    clean = [(ts, value) for ts, value in clean if counts[ts] == 1]
    if not clean:
        return {"status": "collecting", "reason": "duplicate_timestamps_only", "malformed_samples": malformed}

    segment = [clean[0]]
    for point in clean[1:]:
        if point[0] - segment[-1][0] > MAX_GAP_SECONDS:
            segment = [point]
        else:
            segment.append(point)
    latest_ts, latest_value = segment[-1]
    baseline = segment[:-1][-max(1, int(window)):]
    if len(baseline) < minimum:
        return {
            "status": "collecting", "reason": "insufficient_contiguous_baseline",
            "baseline_samples": len(baseline), "latest_timestamp": latest_ts,
            "latest_value": latest_value, "malformed_samples": malformed,
        }

    values = [value for _, value in baseline]
    center = median(values)
    mad = median([abs(value - center) for value in values])
    robust_scale = MAD_SCALE * mad
    deviation = latest_value - center
    if robust_scale > 0:
        score = deviation / robust_scale
    else:
        # Constant baselines have no estimable spread. Exact equality is normal;
        # any movement is flagged for review, never assigned an infinite score.
        score = 0.0 if deviation == 0 else None
    anomalous = abs(deviation) > 0 if robust_scale == 0 else abs(score) >= 3.5
    return {
        "status": "scored",
        "baseline_samples": len(baseline),
        "baseline_median": round(center, 4),
        "baseline_mad": round(mad, 4),
        "latest_timestamp": latest_ts,
        "latest_value": round(latest_value, 4),
        "deviation": round(deviation, 4),
        "robust_z_score": round(score, 4) if score is not None else None,
        "anomaly": bool(anomalous),
        "classification": "review_anomaly" if anomalous else "within_baseline",
        "malformed_samples": malformed,
        "method": "contiguous rolling median/MAD; latest sample excluded from baseline",
        "mode": "shadow_only",
        "safety": "screening signal only; no fuel estimate or boiler control changes",
    }
