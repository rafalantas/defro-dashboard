"""Conservative, read-only event correlation for timestamped boiler telemetry.

Flue episodes are screening candidates, never proof of burner ignition. All
cross-signal summaries use real timestamps and bounded windows; gaps are not
interpolated. Nothing in this module is connected to boiler controls or fuel
estimates.
"""
from __future__ import annotations

from datetime import datetime, timedelta

MAX_GAP_SECONDS = 300
RETURN_WARNING_C = 54.0
FLUE_ACTIVE_C = 70.0
FLUE_COOL_C = 50.0
EVENT_CONTEXT_MINUTES = 10
PUMP_KEYS = ("pump_floor", "pump_dhw", "pump_circulation")
CONTEXT_KEYS = (
    "co_c", "dhw_c", "floor_c", "return_c", "valve_opening_pct",
    "valve_setpoint_c", "boiler_power_kw", *PUMP_KEYS,
)


def _stats(values):
    values = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not values:
        return {"count": 0, "min": None, "mean": None, "median": None, "max": None}
    mid = len(values) // 2
    med = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {
        "count": len(values), "min": round(values[0], 2),
        "mean": round(sum(values) / len(values), 2),
        "median": round(med, 2), "max": round(values[-1], 2),
    }


def _normalize(points):
    normalized = {}
    for key, samples in points.items():
        clean = [(ts, float(value)) for ts, value in samples
                 if ts is not None and isinstance(value, (int, float))]
        normalized[key] = sorted(clean, key=lambda item: item[0])
    return normalized


def _quality(normalized):
    result = {}
    for key, samples in normalized.items():
        gaps = [(b[0] - a[0]).total_seconds() for a, b in zip(samples, samples[1:])]
        result[key] = {
            "samples": len(samples),
            "first": samples[0][0].isoformat() if samples else None,
            "last": samples[-1][0].isoformat() if samples else None,
            "gaps_over_5m": sum(g > MAX_GAP_SECONDS for g in gaps),
            "max_gap_seconds": round(max(gaps), 1) if gaps else None,
        }
    return result


def _episodes(samples, predicate, close_predicate=None):
    """Group matching samples; split on telemetry gaps and optional close signal."""
    found, current = [], []
    for ts, value in samples:
        if current and (ts - current[-1][0]).total_seconds() > MAX_GAP_SECONDS:
            found.append(current)
            current = []
        if predicate(value):
            current.append((ts, value))
        elif current and close_predicate and close_predicate(value):
            found.append(current)
            current = []
    if current:
        found.append(current)
    return found


def _window_stats(normalized, start, end):
    result = {}
    for key in CONTEXT_KEYS:
        samples = [(ts, value) for ts, value in normalized.get(key, [])
                   if start <= ts <= end]
        values = [value for _, value in samples]
        result[key] = _stats(values)
        result[key]["first"] = samples[0][0].isoformat() if samples else None
        result[key]["last"] = samples[-1][0].isoformat() if samples else None
    return result


def _trend(samples):
    if len(samples) < 2:
        return None
    elapsed = (samples[-1][0] - samples[0][0]).total_seconds() / 60
    if elapsed <= 0:
        return None
    return round((samples[-1][1] - samples[0][1]) / elapsed, 2)



def _fire_events(normalized):
    """Build flame-presence episodes from the dedicated ThingSpeak channel.

    A flame-on sample proves sensor-reported presence, not the controller's
    ignition command. Gaps are never bridged or interpreted as flame-off.
    """
    states = normalized.get("fire_detected", [])
    brightness = normalized.get("fire_brightness", [])
    brightness_by_time = {ts: value for ts, value in brightness}
    events = []
    current = []
    current_appearance_transition = False
    previous_state = None
    previous_ts = None

    def finish(ended_by, end_confirmed=False):
        if not current:
            return
        start_ts, _ = current[0]
        end_ts, _ = current[-1]
        values = [brightness_by_time[ts] for ts, _ in current if ts in brightness_by_time]
        events.append({
            "start": start_ts.isoformat(),
            "end": end_ts.isoformat(),
            "duration_minutes": round((end_ts - start_ts).total_seconds() / 60, 1),
            "samples": len(current),
            "brightness": _stats(values),
            "flame_presence_observed": True,
            "flame_appearance_transition_observed": (
                current_appearance_transition
            ),
            "flame_disappearance_transition_observed": end_confirmed,
            "ended_by": ended_by,
            "classification": "sensor_reported_flame_presence",
            "ignition_command_confirmed": False,
        })
        current.clear()

    for ts, value in states:
        state = value >= 0.5
        if previous_ts is not None and (ts - previous_ts).total_seconds() > MAX_GAP_SECONDS:
            finish("telemetry_gap")
            previous_state = None
        if state:
            if not current:
                current_appearance_transition = previous_state is False and previous_ts is not None
                current.append((ts, value))
            else:
                current.append((ts, value))
        elif current:
            finish("flame_signal_off", end_confirmed=True)
        previous_state = state
        previous_ts = ts

    finish("latest_sample_active" if current else "series_end")
    return events



def _ignition_requests(normalized):
    """Detect a controller ignition request from the live boiler hysteresis threshold."""
    temperatures = {ts: value for ts, value in normalized.get("boiler_temp_c", [])}
    setpoints = {ts: value for ts, value in normalized.get("boiler_setpoint_c", [])}
    hysteresis = {ts: value for ts, value in normalized.get("boiler_hysteresis_c", [])}
    samples = sorted(set(temperatures) & set(setpoints) & set(hysteresis))
    requests = []
    previous = None
    for ts in samples:
        temp, target, band = temperatures[ts], setpoints[ts], hysteresis[ts]
        threshold = target - band
        if previous is not None:
            prev_ts, prev_temp, prev_threshold = previous
            gap = (ts - prev_ts).total_seconds()
            if 0 < gap <= MAX_GAP_SECONDS and prev_temp > prev_threshold and temp <= threshold:
                requests.append({
                    "at": ts.isoformat(),
                    "boiler_temp_c": round(temp, 2),
                    "setpoint_c": round(target, 2),
                    "hysteresis_c": round(band, 2),
                    "ignition_threshold_c": round(threshold, 2),
                    "classification": "controller_ignition_request_by_hysteresis",
                    "source": "observed_boiler_temperature_crossing",
                })
        previous = (ts, temp, threshold)
    return requests


def _ignition_cycles(normalized, flame_events, flue_events, requests):
    """Correlate each hysteresis request with flame, flue response and temperature recovery."""
    temps = normalized.get("boiler_temp_c", [])
    setpoints = {ts: value for ts, value in normalized.get("boiler_setpoint_c", [])}
    cycles = []
    for request in requests:
        start = datetime.fromisoformat(request["at"])
        flame = next((event for event in flame_events
                      if datetime.fromisoformat(event["start"]) >= start), None)
        flue = next((event for event in flue_events
                     if datetime.fromisoformat(event["start"]) >= start), None)
        recovery = next(((ts, value) for ts, value in temps
                         if ts > start and ts in setpoints and value >= setpoints[ts]), None)
        def elapsed(event_time):
            return round((event_time - start).total_seconds() / 60, 1)
        cycles.append({
            **request,
            "flame_detected_at": flame["start"] if flame else None,
            "minutes_to_flame": elapsed(datetime.fromisoformat(flame["start"])) if flame else None,
            "flue_activity_at": flue["start"] if flue else None,
            "minutes_to_flue_activity": elapsed(datetime.fromisoformat(flue["start"])) if flue else None,
            "temperature_recovered_at": recovery[0].isoformat() if recovery else None,
            "minutes_to_temperature_recovery": elapsed(recovery[0]) if recovery else None,
            "recovered_boiler_temp_c": round(recovery[1], 2) if recovery else None,
            "flame_presence_observed": flame is not None,
            "flue_activity_observed": flue is not None,
            "temperature_recovery_observed": recovery is not None,
        })
    return cycles

def build_raw_diagnostics(points, now):
    """Summarize raw samples, flue candidates and clustered low-return episodes."""
    normalized = _normalize(points)
    quality = _quality(normalized)
    flue = normalized.get("flue_c", [])
    firing_candidates = _episodes(flue, lambda value: value >= FLUE_ACTIVE_C,
                                  lambda value: value <= FLUE_COOL_C)
    firing = []
    context_delta = timedelta(minutes=EVENT_CONTEXT_MINUTES)
    for episode in firing_candidates:
        start, end = episode[0][0], episode[-1][0]
        context = _window_stats(normalized, start - context_delta, end + context_delta)
        power = context["boiler_power_kw"]
        firing.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "duration_minutes": round((end - start).total_seconds() / 60, 1),
            "flue_c": _stats([value for _, value in episode]),
            "flue_trend_c_per_min": _trend(episode),
            "context_window_minutes_each_side": EVENT_CONTEXT_MINUTES,
            "context": context,
            "evidence": {
                "boiler_power_observed": power["count"] > 0,
                "boiler_power_above_1kw": power["max"] is not None and power["max"] > 1.0,
                "boiler_power_peak_kw": power["max"],
            },
            "classification": "elevated_flue_activity",
            "ignition_confirmed": False,
        })

    fire_events = _fire_events(normalized)
    ignition_requests = _ignition_requests(normalized)
    ignition_cycles = _ignition_cycles(normalized, fire_events, firing, ignition_requests)

    returns = normalized.get("return_c", [])
    low_samples = [(ts, value) for ts, value in returns if value < RETURN_WARNING_C]
    low_groups = _episodes(
        returns,
        lambda value: value < RETURN_WARNING_C,
        lambda value: value >= RETURN_WARNING_C,
    )
    low_episodes = []
    for episode in low_groups:
        start, end = episode[0][0], episode[-1][0]
        context = _window_stats(normalized, start - context_delta, end + context_delta)
        low_episodes.append({
            "start": start.isoformat(), "end": end.isoformat(),
            "duration_minutes": round((end - start).total_seconds() / 60, 1),
            "samples": len(episode),
            "return_c": _stats([value for _, value in episode]),
            "context": context,
            "code": "return_below_54c_episode",
            "severity": "warning",
            "note": "Sygnał do przeglądu; nie potwierdza zadziałania ani awarii ochrony powrotu.",
        })

    low_return = [{
        "at": ts.isoformat(), "return_c": round(value, 2),
        "code": "return_below_54c", "severity": "warning",
    } for ts, value in low_samples]
    hydraulics = {
        "return_c": _stats([value for _, value in returns]),
        "valve_opening_pct": _stats([value for _, value in normalized.get("valve_opening_pct", [])]),
        "boiler_power_kw": _stats([value for _, value in normalized.get("boiler_power_kw", [])]),
        "pump_state_samples": {
            key: sum(value >= 0.5 for _, value in normalized.get(key, []))
            for key in PUMP_KEYS
        },
        "low_return_samples": len(low_return),
        "low_return_episode_count": len(low_episodes),
        "low_return_events": low_return[:200],
        "low_return_episodes": low_episodes[-100:],
        "reference_return_protection_c": 55.0,
        "warning_threshold_c": RETURN_WARNING_C,
        "note": "Próbki poniżej 54°C są sygnałem do przeglądu, nie dowodem zadziałania ochrony. Nastawy pozostają bez zmian.",
    }
    return {
        "mode": "read_only_timestamped_event_correlation",
        "timezone": str(now.tzinfo),
        "series": quality,
        "firing_activity": {
            "episodes_count": len(firing),
            "episodes": firing[-100:],
            "threshold_flue_active_c": FLUE_ACTIVE_C,
            "threshold_flue_cool_c": FLUE_COOL_C,
            "max_sample_gap_seconds": MAX_GAP_SECONDS,
            "context_window_minutes_each_side": EVENT_CONTEXT_MINUTES,
            "note": "Epizody spalin są kandydatami do korelacji z hydrauliką, nie potwierdzonymi rozpaleniami.",
        },
        "ignition_activity": {
            "requests_count": len(ignition_requests),
            "requests": ignition_requests[-100:],
            "cycles_count": len(ignition_cycles),
            "cycles": ignition_cycles[-100:],
            "threshold_rule": "boiler_temp_c crosses from above to at-or-below setpoint_c - hysteresis_c",
            "note": "Żądanie rozpalenia jest wnioskiem z przekroczenia progu histerezy; nie jest odczytem komendy sterownika.",
        },
        "fire_activity": {
            "episodes_count": len(fire_events),
            "episodes": fire_events[-100:],
            "max_sample_gap_seconds": MAX_GAP_SECONDS,
            "note": "Czujnik potwierdza obecność płomienia; przejście sygnału nie potwierdza komendy ani sekwencji rozpalania palnika.",
        },
        "hydraulics": hydraulics,
        "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
    }
