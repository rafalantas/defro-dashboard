"""Read-only Fibaro analytics pipeline over persisted shared SQLite evidence."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thermoflow_fibaro_data_quality import assess_fibaro_history_quality
from thermoflow_fibaro_event_history import analyze_fibaro_event_history
from thermoflow_fibaro_shadow_evaluation import evaluate_fibaro_shadow
from thermoflow_fibaro_shadow_ml import train_fibaro_daily_model
from thermoflow_fibaro_time_profile import build_fibaro_time_profile

MAX_PIPELINE_SECONDS = 90 * 86400
MAX_PIPELINE_EVENTS = 100000


def _validate_window(start, end):
    if (type(start) is not int or type(end) is not int or start <= 0
            or end <= start or end - start > MAX_PIPELINE_SECONDS):
        raise ValueError("Fibaro shadow pipeline window must be ordered and at most 90 days")


def _local_day_bounds(day, zone):
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp()) - 1


def run_fibaro_shadow_pipeline(collector, *, start, end, timezone_name="Europe/Warsaw",
                               minimum_events=50, minimum_observed_days=7):
    """Build quality-gated daily features and chronological shadow evaluation.

    Reads only persisted HC3 events and the latest persisted snapshot. It does
    not contact HC3, start collection, write model files, persist predictions,
    or affect heating/fuel control.
    """
    _validate_window(start, end)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("invalid Fibaro pipeline timezone") from None

    snapshot_record = collector.read_latest_snapshot()
    if snapshot_record is None:
        return {
            "schema": "thermoflow-fibaro-shadow-pipeline/1",
            "status": "collecting",
            "reason": "persisted_fibaro_snapshot_required",
            "evidence": {"events": 0, "snapshot_available": False},
            "quality": None,
            "daily_features": [],
            "model": None,
            "backtest": None,
            "safety": {"read_only": True, "hc3_requests": False,
                       "device_commands_sent": False, "fuel_forecast_changed": False},
        }

    events = collector.read_events(start=start, end=end, limit=MAX_PIPELINE_EVENTS)
    if len(events) >= MAX_PIPELINE_EVENTS:
        return {
            "schema": "thermoflow-fibaro-shadow-pipeline/1",
            "status": "blocked",
            "reason": "event_read_limit_reached",
            "evidence": {"events": len(events), "snapshot_available": True},
            "quality": None,
            "daily_features": [],
            "model": None,
            "backtest": None,
            "safety": {"read_only": True, "hc3_requests": False,
                       "device_commands_sent": False, "fuel_forecast_changed": False},
        }

    snapshot = snapshot_record["payload"]
    quality = assess_fibaro_history_quality(
        events, snapshot, start=start, end=end,
        minimum_events=minimum_events,
        minimum_observed_days=minimum_observed_days,
    )

    by_timestamp = [event for event in events
                    if isinstance(event, dict) and type(event.get("timestamp")) is int]
    local_start = datetime.fromtimestamp(start, timezone.utc).astimezone(zone).date()
    local_end = datetime.fromtimestamp(end, timezone.utc).astimezone(zone).date()
    daily_features = []
    hourly_features = []
    current_day = local_start
    while current_day <= local_end:
        day_start, day_end = _local_day_bounds(current_day, zone)
        window_start = max(start, day_start)
        window_end = min(end, day_end)
        if window_start < window_end:
            day_events = [event for event in by_timestamp
                          if window_start <= event["timestamp"] <= window_end]
            history = analyze_fibaro_event_history(
                snapshot, day_events, window_start, window_end
            )
            profile = build_fibaro_time_profile(
                snapshot, history, timezone_name=timezone_name
            )
            daily_features.extend(profile["daily"])
            hourly_features.extend(profile["hourly"])
        current_day += timedelta(days=1)

    # A day can appear only when at least one supported event transition was
    # observed. Missing days are intentionally not synthesized for ML.
    daily_features.sort(key=lambda row: row["date"])
    model = train_fibaro_daily_model(daily_features, quality)
    backtest = evaluate_fibaro_shadow(model)
    status = "evaluated" if backtest.get("status") == "evaluated" else (
        "blocked" if quality.get("status") == "blocked" else "collecting"
    )
    return {
        "schema": "thermoflow-fibaro-shadow-pipeline/1",
        "status": status,
        "mode": "offline_shadow_only",
        "window": {"start": start, "end": end, "timezone": timezone_name},
        "evidence": {
            "events": len(events),
            "snapshot_available": True,
            "snapshot_captured_at": snapshot_record["captured_at"],
            "observed_days_with_transitions": len(daily_features),
            "observed_hour_buckets": len(hourly_features),
        },
        "quality": quality,
        "daily_features": daily_features,
        "model": model,
        "backtest": backtest,
        "safety": {
            "read_only": True,
            "hc3_requests": False,
            "hc3_writes": False,
            "device_commands_sent": False,
            "predictions_persisted": False,
            "automatic_promotion": False,
            "fuel_forecast_changed": False,
            "return_protection": "55°C ±1°C",
        },
    }
