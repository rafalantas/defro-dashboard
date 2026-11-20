"""Read-only seven-stage ThermoFlow diagnostic pipeline (stdlib only)."""
from datetime import datetime, timezone
from math import sqrt, isfinite
from statistics import median

VERSION = "1.0.0"
MAX_GAP_SECONDS = 300
RETURN_PROTECTION_C = 55.0
RETURN_WARNING_C = 54.0
MIN_TRAIN_CYCLES = 20
HOLDOUT_CYCLES = 5


def _points(raw, key):
    result = []
    for point in raw.get(key, []) or []:
        try:
            ts, value = int(point[0]), float(point[1])
            if ts <= 0 or not isfinite(value):
                continue
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        result.append((ts, value))
    return sorted(result)


def _unambiguous_points(raw, key):
    """Discard all samples at duplicate timestamps for event correlation."""
    points = _points(raw, key)
    counts = {}
    for ts, _value in points:
        counts[ts] = counts.get(ts, 0) + 1
    return [(ts, value) for ts, value in points if counts[ts] == 1]


def _quality(raw):
    """Audit raw series before sorting; never hide malformed or duplicate samples."""
    output = {}
    for key in sorted(raw):
        source = raw.get(key, []) or []
        valid = []
        malformed = duplicate_timestamps = out_of_order = 0
        seen = set()
        previous_ts = None
        for point in source:
            try:
                ts, value = int(point[0]), float(point[1])
                if ts <= 0 or not isfinite(value):
                    raise ValueError("invalid sample")
            except (TypeError, ValueError, IndexError, OverflowError):
                malformed += 1
                continue
            if ts in seen:
                duplicate_timestamps += 1
            if previous_ts is not None and ts < previous_ts:
                out_of_order += 1
            seen.add(ts)
            previous_ts = ts
            valid.append((ts, value))
        points = sorted(valid)
        gaps = [b[0] - a[0] for a, b in zip(points, points[1:])]
        output[key] = {
            "samples": len(points), "raw_samples": len(source),
            "malformed_samples": malformed, "duplicate_timestamps": duplicate_timestamps,
            "out_of_order_samples": out_of_order,
            "first": datetime.fromtimestamp(points[0][0], timezone.utc).isoformat() if points else None,
            "last": datetime.fromtimestamp(points[-1][0], timezone.utc).isoformat() if points else None,
            "gaps_over_5m": sum(gap > MAX_GAP_SECONDS for gap in gaps),
            "max_gap_seconds": max(gaps) if gaps else None,
            "coverage_status": "review" if malformed or duplicate_timestamps or out_of_order or any(g > MAX_GAP_SECONDS for g in gaps) else ("available" if points else "empty"),
        }
    return output


def _hydraulics(raw):
    """Detect pump starts/stops and bounded return-temperature associations."""
    returns = _unambiguous_points(raw, "thermoflow-valve4d-return")
    opening = _unambiguous_points(raw, "thermoflow-valve4d-opening")
    pumps = {}
    pump_keys = ("thermoflow-pump-floor", "thermoflow-pump-dhw",
                 "thermoflow-pump-valve4d", "thermoflow-pump-circulation")
    for key in pump_keys:
        samples, events = _unambiguous_points(raw, key), []
        used_return_samples = set()
        for (old_ts, old), (ts, value) in zip(samples, samples[1:]):
            if ts - old_ts > MAX_GAP_SECONDS:
                continue
            old_on, new_on = old >= .5, value >= .5
            if old_on == new_on:
                continue
            event_type = "start" if new_on else "stop"
            before_point = next(((t, v) for t, v in reversed(returns)
                                  if t <= ts and ts - t <= MAX_GAP_SECONDS
                                  and t not in used_return_samples), None)
            after_point = next(((t, v) for t, v in returns
                                 if t > ts and t - ts <= MAX_GAP_SECONDS
                                 and t not in used_return_samples), None)
            before = before_point[1] if before_point else None
            after = after_point[1] if after_point else None
            paired = bool(before_point and after_point
                          and after_point[0] - before_point[0] <= 2 * MAX_GAP_SECONDS)
            if paired:
                used_return_samples.update((before_point[0], after_point[0]))
            events.append({
                "event": event_type,
                "at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "return_before_c": before if paired else None,
                "return_after_c": after if paired else None,
                "return_delta_c": round(after - before, 2) if paired else None,
                "return_before_at": datetime.fromtimestamp(before_point[0], timezone.utc).isoformat() if paired else None,
                "return_after_at": datetime.fromtimestamp(after_point[0], timezone.utc).isoformat() if paired else None,
                "association_window_seconds": MAX_GAP_SECONDS,
                "association_status": "paired" if paired else "insufficient_unique_contiguous_return_samples",
            })
        paired = [event["return_delta_c"] for event in events if event["return_delta_c"] is not None]
        pumps[key] = {
            "starts": sum(event["event"] == "start" for event in events),
            "stops": sum(event["event"] == "stop" for event in events),
            "paired_events": len(paired),
            "mean_return_delta_c": round(sum(paired) / len(paired), 2) if paired else None,
            "events": events[-60:],
        }
    low = [value for _, value in returns if value < RETURN_WARNING_C]
    return {
        "return_samples": len(returns),
        "return_min_c": min((value for _, value in returns), default=None),
        "return_below_54c_samples": len(low),
        "reference_return_protection_c": RETURN_PROTECTION_C,
        "warning_threshold_c": RETURN_WARNING_C,
        "valve_opening_samples": len(opening), "pump_correlations": pumps,
        "interpretation": "Korelacja czasowa, nie dowód przyczynowości. Próbki poniżej 54°C są sygnałem do przeglądu, nie potwierdzają zadziałania ochrony.",
    }


def _fire_diagnostics(raw):
    """Interpret FIRE state and brightness independently; never infer flame from brightness."""
    state_present = "thermoflow-fire" in raw
    brightness_present = "thermoflow-fire-brightness" in raw
    state_raw = raw.get("thermoflow-fire", []) or []
    brightness_raw = raw.get("thermoflow-fire-brightness", []) or []
    state = _points(raw, "thermoflow-fire")
    brightness = _points(raw, "thermoflow-fire-brightness")
    invalid_state = sum(1 for point in state_raw if _valid_numeric_point(point) and float(point[1]) not in (0.0, 1.0))
    invalid_brightness = sum(1 for point in brightness_raw if not _valid_numeric_point(point) or float(point[1]) < 0)
    valid_state = [(ts, value) for ts, value in state if value in (0.0, 1.0)]
    status = "available" if valid_state or brightness else (
        "empty" if state_present or brightness_present else "not_provided"
    )
    return {
        "status": status,
        "source": "dedicated_thingspeak_fire_channel",
        "flame_state": {
            "status": "available" if valid_state else ("invalid_or_empty" if state_present else "not_provided"),
            "samples": len(state_raw), "valid_binary_samples": len(valid_state),
            "on_samples": sum(value == 1 for _, value in valid_state),
            "off_samples": sum(value == 0 for _, value in valid_state),
            "invalid_samples": invalid_state,
            "last_value": valid_state[-1][1] if valid_state else None,
        },
        "brightness": {
            "status": "available" if brightness else ("empty" if brightness_present else "not_provided"),
            "samples": len(brightness_raw),
            "min": min((value for _, value in brightness), default=None),
            "max": max((value for _, value in brightness), default=None),
            "mean": round(sum(value for _, value in brightness) / len(brightness), 2) if brightness else None,
            "invalid_samples": invalid_brightness,
            "note": "Jasność jest analizowana niezależnie; nie zastępuje binarnego stanu płomienia.",
        },
        "quality": {
            "status": "review" if invalid_state or invalid_brightness else ("available" if valid_state or brightness else "insufficient"),
            "invalid_flame_state_samples": invalid_state,
            "invalid_brightness_samples": invalid_brightness,
        },
        "note": "Brak próbek nie rozstrzyga, czy kanał nie jest skonfigurowany, czy odczyt zakończył się błędem; API nie przekazuje tu przyczyny fetch.",
    }


def _valid_numeric_point(point):
    try:
        return len(point) >= 2 and isfinite(float(point[1]))
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


def _combustion(raw, fire_diagnostics=None):
    flue = _points(raw, "thermoflow-flue")
    fire = [(ts, value) for ts, value in _points(raw, "thermoflow-fire") if value in (0.0, 1.0)]
    brightness = _points(raw, "thermoflow-fire-brightness")
    episodes, current, previous = [], [], None
    for ts, value in flue:
        if previous is not None and ts - previous > MAX_GAP_SECONDS and current:
            episodes.append(current); current = []
        if value >= 70:
            current.append((ts, value))
        elif value <= 50 and current:
            episodes.append(current); current = []
        previous = ts
    if current:
        episodes.append(current)
    correlations = []
    legacy_correlations = []
    for episode in episodes[-100:]:
        start_ts, end_ts = episode[0][0], episode[-1][0]
        nearby_state = [(ts, value) for ts, value in fire if start_ts - 300 <= ts <= end_ts + 300]
        nearby_brightness = [(ts, value) for ts, value in brightness if start_ts - 300 <= ts <= end_ts + 300]
        correlations.append({
            "flue_start": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
            "fire_state_samples": len(nearby_state),
            "fire_on_samples": sum(value == 1 for _, value in nearby_state),
            "fire_off_samples": sum(value == 0 for _, value in nearby_state),
            "brightness_samples": len(nearby_brightness),
            "interpretation": "correlation_only; flame-state evidence and brightness remain separate",
        })
        legacy_correlations.append({
            "flue_start": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
            "fire_samples_within_5m": len(nearby_state),
            "nearest_fire_sample": ({"at": datetime.fromtimestamp(nearby_state[0][0], timezone.utc).isoformat(),
                                     "value": nearby_state[0][1]} if nearby_state else None),
            "interpretation": "timestamp_overlap_only; FIRE value semantics are not inferred",
        })
    return {
        "flue_samples": len(flue),
        "fire_samples": len(fire),
        "fire_brightness_samples": len(brightness),
        "fire_join_status": "available" if fire else "not_provided",
        "fire_flue_correlations": correlations,
        "fire_flue_time_correlations": legacy_correlations,
        "elevated_flue_episodes": [{
            "start": datetime.fromtimestamp(ep[0][0], timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(ep[-1][0], timezone.utc).isoformat(),
            "duration_minutes": round((ep[-1][0] - ep[0][0]) / 60, 1),
            "flue_min_c": min(v for _, v in ep), "flue_max_c": max(v for _, v in ep),
            "classification": "elevated_flue_activity", "ignition_confirmed": False,
        } for ep in episodes[-100:]],
        "fire_sensor": {
            **(fire_diagnostics or _fire_diagnostics(raw)),
            "status": ("available_in_thermoflow_history" if fire else "not_available_in_history_window"),
            "fields": {"field1": "flame_state", "field2": "brightness"},
            "note": ("FIRE został dołączony do historii ThermoFlow z osobnego kanału ThingSpeak."
                     if fire else "Brak próbek FIRE w tym zakresie historii."),
        },
        "note": "Podwyższona temperatura spalin jest kandydatem do korelacji, nie potwierdzeniem rozpalenia.",
    }


def _burner_cycles(raw, fire_diagnostics):
    """Build conservative burner cycles; telemetry gaps censor rather than join cycles."""
    parsed_points = [(ts, value) for ts, value in _points(raw, "thermoflow-fire")
                     if value in (0.0, 1.0)]
    timestamp_counts = {}
    for timestamp, _value in parsed_points:
        timestamp_counts[timestamp] = timestamp_counts.get(timestamp, 0) + 1
    duplicate_timestamp_samples = sum(count for count in timestamp_counts.values() if count > 1)
    points = [(ts, value) for ts, value in parsed_points if timestamp_counts[ts] == 1]
    raw_fire_samples = raw.get("thermoflow-fire", []) or []
    discarded_non_binary_samples = max(0, len(raw_fire_samples) - len(parsed_points))
    if not points:
        return {"status": "collecting", "cycles": [], "complete": 0, "incomplete": 0,
                "incomplete_cycles": [], "ignored_transitions": 0,
                "duplicate_timestamp_samples": duplicate_timestamp_samples,
                "discarded_non_binary_samples": discarded_non_binary_samples,
                "note": "Brak jednoznacznych binarnych próbek FIRE; cykle palnika pozostają nieznane."}

    # Split the timeline at every telemetry gap. A state observed after a gap
    # must never be joined to a cycle from before that gap.
    segments, segment = [], [points[0]]
    for point in points[1:]:
        if point[0] - segment[-1][0] > MAX_GAP_SECONDS:
            segments.append(segment)
            segment = [point]
        else:
            segment.append(point)
    if segment:
        segments.append(segment)

    flue = _points(raw, "thermoflow-flue")
    power = _points(raw, "thermoflow-boiler-power")
    cycles, incomplete_cycles = [], []
    ignored_transitions = 0

    def evidence(series, opened, closed):
        return any(opened - 300 <= timestamp <= closed + 300 and value > 0
                   for timestamp, value in series)

    for segment_index, samples in enumerate(segments):
        state = samples[0][1]
        opened = samples[0][0] if state == 1 else None
        left_censored = opened is not None
        for index in range(1, len(samples)):
            previous, current = samples[index - 1], samples[index]
            if current[1] == previous[1]:
                continue
            # Accept a transition only when the new state is repeated by the
            # next contiguous sample. A lone spike is explicitly ignored.
            if index + 1 >= len(samples):
                ignored_transitions += 1
                continue
            confirm = samples[index + 1]
            if confirm[0] - current[0] > MAX_GAP_SECONDS or confirm[1] != current[1]:
                ignored_transitions += 1
                continue

            if current[1] == 1:
                if opened is None:
                    opened = current[0]
                    left_censored = False
            elif opened is not None:
                if left_censored:
                    incomplete_cycles.append({
                        "start": datetime.fromtimestamp(opened, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(current[0], timezone.utc).isoformat(),
                        "complete": False, "reason": "left_censored_initial_on_state",
                        "duration_minutes": None,
                    })
                else:
                    flue_support = evidence(flue, opened, current[0])
                    power_support = evidence(power, opened, current[0])
                    cycles.append({
                        "start": datetime.fromtimestamp(opened, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(current[0], timezone.utc).isoformat(),
                        "duration_minutes": round((current[0] - opened) / 60, 1),
                        "flue_support": flue_support, "power_support": power_support,
                        "status": "confirmed_by_fire_state",
                        "complete": True,
                    })
                opened = None
                left_censored = False

        if opened is not None:
            reason = "telemetry_gap" if segment_index < len(segments) - 1 else "right_censored_open_cycle"
            incomplete_cycles.append({
                "start": datetime.fromtimestamp(opened, timezone.utc).isoformat(),
                "end": datetime.fromtimestamp(samples[-1][0], timezone.utc).isoformat(),
                "complete": False, "reason": reason, "duration_minutes": None,
            })

    return {
        "status": "available" if cycles else ("partial" if incomplete_cycles else "collecting"),
        "cycles": cycles[-100:], "complete": len(cycles),
        "incomplete": len(incomplete_cycles),
        "incomplete_cycles": incomplete_cycles[-100:],
        "open_cycles": [item for item in incomplete_cycles if item["end"] is not None][-100:],
        "ignored_transitions": ignored_transitions,
        "duplicate_timestamp_samples": duplicate_timestamp_samples,
        "discarded_non_binary_samples": discarded_non_binary_samples,
        "duration_validation": "strictly_positive_elapsed_time; censored_cycles_excluded",
        "transition_debounce": "two_consecutive_samples",
        "max_gap_seconds": MAX_GAP_SECONDS,
        "timeline_segments": len(segments),
        "note": "Luki telemetryczne rozcinają oś czasu; cykle ucięte lub niejednoznaczne nie trafiają do baseline. Pojedyncze przełączenia są ignorowane.",
    }

def _cycle_stage(base, burner_cycles=None):
    """Keep legacy 4D protection cycles separate from burner cycles."""
    cycles = (base or {}).get("protection_cycles", [])
    complete = [c for c in cycles if c.get("complete") and isinstance(c.get("cold_gap_minutes"), (int, float))]
    return ({"status": "available" if cycles else "collecting",
             "source": "existing_timestamped_4d_protection_cycle_analysis",
             "cycles": cycles[-100:], "total": len(cycles), "complete": len(complete),
             "incomplete": len(cycles) - len(complete),
             "burner_cycle_count": (burner_cycles or {}).get("complete", 0),
             "ml_metric": "cold_gap_minutes",
             "note": "Cykle ochrony zaworu 4D są oddzielone od cykli palnika."}, complete)

def _ml(complete):
    if len(complete) < MIN_TRAIN_CYCLES + HOLDOUT_CYCLES:
        return {"status": "collecting", "method": "robust_median_mad",
                "complete_cycles": len(complete), "required_cycles": MIN_TRAIN_CYCLES + HOLDOUT_CYCLES,
                "training_cycles": 0, "holdout_cycles": 0,
                "reason": "Za mało kompletnych cykli do walidacji chronologicznej; model nie jest trenowany."}
    train, holdout = complete[:-HOLDOUT_CYCLES], complete[-HOLDOUT_CYCLES:]
    values = [float(c["cold_gap_minutes"]) for c in train]
    center = median(values)
    mad = median([abs(v - center) for v in values])
    scale = max(1.0, mad * 1.4826)
    errors, anomalies = [], []
    for cycle in holdout:
        actual = float(cycle["cold_gap_minutes"])
        error = center - actual
        errors.append(error)
        score = abs(actual - center) / scale
        if score >= 3.5:
            anomalies.append({"protection_start": cycle.get("protection_start"),
                              "cold_gap_minutes": actual, "robust_deviation": round(score, 2),
                              "severity": "review"})
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = sqrt(sum(error * error for error in errors) / len(errors))
    bias = sum(errors) / len(errors)
    return {"status": "evaluated", "method": "robust_median_mad",
            "training_cycles": len(train), "holdout_cycles": len(holdout),
            "baseline_cold_gap_minutes": {"median": round(center, 2), "mad": round(mad, 2)},
            "holdout_metrics": {"mae_minutes": round(mae, 3), "rmse_minutes": round(rmse, 3),
                                "bias_minutes": round(bias, 3), "observations": len(errors),
                                "prediction": "training_median_baseline"},
            "holdout_anomalies": anomalies, "promotion": "disabled_shadow_only",
            "validation": "chronological_last_5_cycles_holdout",
            "note": "Metryki opisują naiwną prognozę medianą treningową; nie dowodzą gotowości modelu produkcyjnego."}



def _burner_ml(burner_cycles):
    """Chronological descriptive baseline for validated burner on-duration cycles."""
    complete = [cycle for cycle in (burner_cycles or {}).get("cycles", [])
                if cycle.get("complete") and isinstance(cycle.get("duration_minutes"), (int, float))]
    if len(complete) < MIN_TRAIN_CYCLES + HOLDOUT_CYCLES:
        return {"status": "collecting", "method": "training_median_baseline",
                "complete_burner_cycles": len(complete),
                "required_cycles": MIN_TRAIN_CYCLES + HOLDOUT_CYCLES,
                "training_cycles": 0, "holdout_cycles": 0,
                "reason": "Za mało kompletnych cykli palnika do chronologicznej walidacji baseline."}
    train, holdout = complete[:-HOLDOUT_CYCLES], complete[-HOLDOUT_CYCLES:]
    values = [float(cycle["duration_minutes"]) for cycle in train]
    center = median(values)
    errors = [center - float(cycle["duration_minutes"]) for cycle in holdout]
    mae = sum(abs(error) for error in errors) / len(errors)
    rmse = sqrt(sum(error * error for error in errors) / len(errors))
    bias = sum(errors) / len(errors)
    return {"status": "evaluated", "method": "training_median_baseline",
            "training_cycles": len(train), "holdout_cycles": len(holdout),
            "baseline_duration_minutes": round(center, 2),
            "holdout_metrics": {"mae_minutes": round(mae, 3), "rmse_minutes": round(rmse, 3),
                                "bias_minutes": round(bias, 3), "observations": len(errors)},
            "validation": "chronological_last_5_burner_cycles_holdout",
            "promotion": "disabled_shadow_only",
            "note": "Naiwny baseline medianowy czasu pracy palnika, nie wytrenowany model ML."}


def _prediction(complete, ml):
    if ml.get("status") != "evaluated":
        return {"status": "unavailable",
                "reason": "Wymagane co najmniej 20 cykli treningowych i 5 chronologicznych cykli testowych."}
    values = [float(c["cold_gap_minutes"]) for c in complete[:-HOLDOUT_CYCLES]]
    center = median(values)
    mad = median([abs(v - center) for v in values])
    return {"status": "experimental", "expected_cold_gap_minutes": round(center, 1),
            "typical_range_minutes": [round(max(0, center - 2 * mad), 1), round(center + 2 * mad, 1)],
            "training_cycles": len(values), "holdout_cycles": HOLDOUT_CYCLES,
            "note": "Opisowy zakres z mediany i MAD, nie prognoza sterująca."}


def build_thermoflow_intelligence(raw, base_analysis=None, now=None):
    """Build all seven diagnostic stages; source data and control outputs stay untouched."""
    now = now or datetime.now(timezone.utc)
    quality = _quality(raw)
    core = {"status": "available" if any(v["samples"] for v in quality.values()) else "collecting",
            "version": VERSION, "timezone": str(now.tzinfo), "series": quality,
            "max_gap_seconds": MAX_GAP_SECONDS, "interpolation": False}
    fire_diagnostics = _fire_diagnostics(raw)
    burner_cycles = _burner_cycles(raw, fire_diagnostics)
    cycles, complete = _cycle_stage(base_analysis, burner_cycles)
    hydraulics, combustion = _hydraulics(raw), _combustion(raw, fire_diagnostics)
    ml = _ml(complete)
    burner_ml = _burner_ml(burner_cycles)
    prediction = _prediction(complete, ml)
    explanations = []
    if hydraulics["return_below_54c_samples"]:
        explanations.append({"code": "return_below_54c", "severity": "warning",
                             "samples": hydraulics["return_below_54c_samples"],
                             "message": "Wystąpiły próbki powrotu poniżej 54°C; sprawdź przebieg pomp i zaworu."})
    if cycles["incomplete"]:
        explanations.append({"code": "incomplete_4d_cycles", "severity": "info",
                             "count": cycles["incomplete"],
                             "message": "Część cykli 4D nie ma zarejestrowanego końca."})
    if combustion["elevated_flue_episodes"]:
        explanations.append({"code": "elevated_flue_activity", "severity": "info",
                             "count": len(combustion["elevated_flue_episodes"]),
                             "message": "Wykryto epizody podwyższonej temperatury spalin; nie są one samodzielnym potwierdzeniem rozpalenia."})
    if ml.get("holdout_metrics"):
        explanations.append({"code": "ml_holdout_evaluated", "severity": "info",
                             "metrics": ml["holdout_metrics"],
                             "message": "Baseline medianowy został oceniony na chronologicznym holdoucie; wynik nie oznacza walidacji modelu produkcyjnego."})
    quality_issues = sum(item.get("malformed_samples", 0) + item.get("duplicate_timestamps", 0)
                         + item.get("out_of_order_samples", 0) for item in quality.values())
    if quality_issues:
        explanations.append({"code": "telemetry_integrity_issues", "severity": "warning",
                             "count": quality_issues,
                             "message": "W surowych szeregach wykryto próbki niepoprawne, zduplikowane lub poza kolejnością."})
    if burner_cycles["status"] == "available":
        explanations.append({"code": "burner_cycles", "severity": "info",
                             "count": burner_cycles["complete"],
                             "message": "Cykle palnika wyznaczono z binarnego FIRE z debounce; spaliny i moc służą jako dowody krzyżowe."})
    if fire_diagnostics["quality"]["status"] == "review":
        explanations.append({"code": "fire_data_quality", "severity": "warning",
                             "details": fire_diagnostics["quality"],
                             "message": "W danych FIRE wykryto wartości spoza oczekiwanego zakresu lub niepoprawne próbki."})
    evidence = [{"id": item["code"], "severity": item["severity"],
                 "source": "thermoflow_diagnostic_pipeline", "message": item["message"]}
                for item in explanations]
    ai = {"status": "evidence_available" if explanations else "no_findings",
          "explanations": explanations, "evidence": evidence,
          "confidence": "limited" if ml["status"] != "evaluated" else "moderate",
          "conflicts": [],
          "note": "Warstwowy silnik regułowy, nie LLM. Pewność jest jakościowa, nie skalibrowana; brak dowodu nie oznacza braku problemu."}
    return {"version": VERSION, "mode": "read_only", "diagnostic_core": core,
            "cycle_analyzer": cycles, "burner_cycle_analyzer": burner_cycles,
            "burner_ml_baseline": burner_ml,
            "fire_diagnostics": fire_diagnostics,
            "hydraulic_diagnostics": hydraulics,
            "combustion_diagnostics": combustion, "ml_baseline": ml,
            "predictive_diagnostics": prediction, "thermoflow_ai": ai,
            "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes"}
