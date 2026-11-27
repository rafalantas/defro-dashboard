"""How good is the production pellet forecast, judged against what was actually burnt?

Read-only. The forecast journal already exists: every time the fuel summary is built,
``persist_weather_forecasts`` archives the model's daily estimate
(``estimated_pellet_kg_day``) together with the moment it was issued, and a new row is
stored only when the payload changed. The latest row issued at or before a moment
therefore *is* the forecast that was in force then, so each day can be judged against the
forecast as it stood a given number of hours before the day began, never against a
re-fitted one.

For every completed day with a counter-based actual, three forecasts are compared with
the actual: the day-ahead forecast (last one issued before the day began) and the last
ones issued at least 24 h and 48 h before. Two naive baselines are scored on the same
days, because a model is only worth having if it beats them: "same as yesterday" and "mean
of the previous seven days".

Days whose counter factor is still provisional (``estimate``) are listed but excluded from
the scores. Nothing is written and no model is changed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from math import isfinite
from pathlib import Path

SCHEMA = "thermoflow-fuel-prediction-accuracy/1"
WEATHER_SOURCE = "open-meteo"
BANDS = (("day_ahead", 0), ("24h", 24), ("48h", 48))      # minimum lead time in hours
PRIMARY_BAND = "day_ahead"
MIN_DAYS_FOR_VERDICT = 7
BASELINE_MIN_HISTORY = 3
SKILL_MARGIN = 0.10
GOOD_ERROR_KG = 2.0


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def read_forecast_archive(database_path, *, start, end):
    """{forecast_for epoch: [(fetched_at, estimated_kg, t_mean), ...]} oldest issue first."""
    path = Path(database_path)
    if not path.is_file():
        return {}
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = db.execute(
            "SELECT forecast_for,fetched_at,payload_json FROM weather_forecasts "
            "WHERE source=? AND forecast_for>=? AND forecast_for<=? ORDER BY forecast_for,fetched_at,id",
            (WEATHER_SOURCE, int(start), int(end))).fetchall()
    finally:
        db.close()
    archive = {}
    for forecast_for, fetched_at, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        kg = _number(payload.get("estimated_pellet_kg_day")) if isinstance(payload, dict) else None
        if kg is None:
            continue
        archive.setdefault(int(forecast_for), []).append(
            (int(fetched_at), kg, _number(payload.get("temperature_mean_c"))))
    return archive


def _as_issued(entries, day_start, min_lead_hours):
    """Latest forecast issued at least ``min_lead_hours`` before the day began."""
    cutoff = day_start - min_lead_hours * 3600
    picked = None
    for fetched_at, kg, _t_mean in entries:
        if fetched_at <= cutoff:
            picked = kg
    return picked


def _mae(errors):
    return round(sum(abs(e) for e in errors) / len(errors), 2) if errors else None


def _fmt(value):
    return f"{value:.1f}".replace(".", ",")


def build_accuracy(actual_rows, archive, *, tz, today, days):
    """Judge archived forecasts against counter-based actuals for completed days."""
    actual = {}
    for row in actual_rows if isinstance(actual_rows, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row.get("day")))
        except (TypeError, ValueError):
            continue
        kg = _number(row.get("kg_counter"))
        if kg is not None and day < today:
            actual[day] = (kg, bool(row.get("estimate")))

    ordered = sorted(actual)
    table, errors = [], {name: [] for name, _ in BANDS}
    base_errors = {name: {"yesterday": [], "mean7": []} for name, _ in BANDS}
    for day in ordered[-days:]:
        kg, provisional = actual[day]
        day_start = int(datetime.combine(day, time.min, tzinfo=tz).timestamp())
        entries = archive.get(day_start, [])
        forecasts = {name: _as_issued(entries, day_start, lead) for name, lead in BANDS}
        history = [actual[d][0] for d in ordered if d < day][-7:]
        previous = actual.get(day - timedelta(days=1))
        baselines = {
            "yesterday": previous[0] if previous else None,
            "mean7": round(sum(history) / len(history), 2) if len(history) >= BASELINE_MIN_HISTORY else None,
        }
        table.append({
            "day": day.isoformat(), "actual_kg": round(kg, 2), "provisional": provisional,
            "forecast_kg": {n: (round(v, 2) if v is not None else None) for n, v in forecasts.items()},
            "error_kg": {n: (round(v - kg, 2) if v is not None else None) for n, v in forecasts.items()},
            "baseline_kg": baselines,
        })
        if provisional:
            continue
        for name, _lead in BANDS:
            if forecasts[name] is None:
                continue
            errors[name].append(forecasts[name] - kg)
            for key, value in baselines.items():
                if value is not None:
                    base_errors[name][key].append((value - kg, forecasts[name] - kg))

    bands = {}
    for name, lead in BANDS:
        errs = errors[name]
        item = {"min_lead_hours": lead, "days": len(errs), "mae_kg": _mae(errs),
                "bias_kg": round(sum(errs) / len(errs), 2) if errs else None,
                "within_2kg_pct": round(100 * sum(abs(e) <= GOOD_ERROR_KG for e in errs) / len(errs), 1) if errs else None}
        skill, baseline_mae = {}, {}
        for key, pairs in base_errors[name].items():
            if pairs:
                baseline_mae[key] = _mae([b for b, _m in pairs])
                model_on_same = _mae([m for _b, m in pairs])
                skill[key] = (round(1 - model_on_same / baseline_mae[key], 3)
                              if baseline_mae[key] else None)
        item["baseline_mae_kg"], item["skill_vs"] = baseline_mae, skill
        if len(errs) < MIN_DAYS_FOR_VERDICT:
            item["assessment"] = "collecting"
        elif skill and all(v is not None for v in skill.values()):
            values = list(skill.values())
            item["assessment"] = ("better_than_naive" if all(v >= SKILL_MARGIN for v in values)
                                  else "worse_than_naive" if all(v < 0 for v in values)
                                  else "inconclusive")
        else:
            item["assessment"] = "inconclusive"
        bands[name] = item

    primary = bands[PRIMARY_BAND]
    return {
        "schema": SCHEMA, "mode": "read_only",
        "window_days": days,
        "coverage": {
            "actual_days": len(actual), "scored_days_day_ahead": primary["days"],
            "provisional_days_excluded": sum(1 for d in actual.values() if d[1]),
            "forecast_days_archived": len(archive),
        },
        "bands": bands, "days": table,
        "summary": _summary(primary),
        "method": ("Prognoza z archiwum w chwili wydania (nie dopasowana po fakcie) kontra zużycie z licznika "
                   "podajnika. Doby z tymczasowym współczynnikiem licznika są pokazane, ale nie wchodzą do oceny."),
        "safety": {"read_only": True, "database_modified": False, "model_changed": False},
    }


def _summary(primary):
    n = primary["days"]
    if n < MIN_DAYS_FOR_VERDICT:
        return (f"Za wcześnie na ocenę: do porównania prognozy na następną dobę z faktycznym zużyciem "
                f"jest {n} z {MIN_DAYS_FOR_VERDICT} potrzebnych dób.")
    text = f"Prognoza na następną dobę myli się średnio o {_fmt(primary['mae_kg'])} kg (z {n} dób)"
    base = primary["baseline_mae_kg"]
    if "yesterday" in base:
        text += f"; „tyle co wczoraj” o {_fmt(base['yesterday'])} kg"
    if "mean7" in base:
        text += f", średnia z 7 dni o {_fmt(base['mean7'])} kg"
    verdict = {"better_than_naive": ". Model jest lepszy od obu prostych prognoz.",
               "worse_than_naive": ". Model jest gorszy od obu prostych prognoz.",
               "inconclusive": ". Wynik jest niejednoznaczny względem prostych prognoz."}[primary["assessment"]]
    return text + verdict


MIN_WINDOW_DAYS, MAX_WINDOW_DAYS = 3, 120


def accuracy_from_engine(engine, days, *, database_path, tz_name="Europe/Warsaw", now=None):
    """(status, payload): actuals from the fuel engine's daily rows, forecasts from the archive."""
    from zoneinfo import ZoneInfo
    if type(days) is not int or not MIN_WINDOW_DAYS <= days <= MAX_WINDOW_DAYS:
        return 400, {"error": f"days musi być liczbą od {MIN_WINDOW_DAYS} do {MAX_WINDOW_DAYS}"}
    tz = ZoneInfo(tz_name)
    today = (now or datetime.now(tz)).astimezone(tz).date()
    result = engine.handle("GET", "/api/fuel/daily", {"days": [str(days)]}, {}, b"")
    if result is None:
        return 502, {"error": "Silnik paliwa nie zwrócił dobowego zużycia"}
    status, payload = result
    if status != 200 or not isinstance(payload, dict):
        return status, (payload if isinstance(payload, dict) else {"error": "Nieprawidłowa odpowiedź silnika paliwa"})
    start = int(datetime.combine(today - timedelta(days=days + 1), time.min, tzinfo=tz).timestamp())
    end = int(datetime.combine(today, time.min, tzinfo=tz).timestamp())
    archive = read_forecast_archive(database_path, start=start, end=end)
    return 200, build_accuracy(payload.get("days") or [], archive, tz=tz, today=today, days=days)
