"""Śledzenie zużycia peletu dla defro-dashboard (tylko biblioteka standardowa).

Dosypki są trzymane w osobnym kanale ThingSpeak, agregaty dzienne tylko w pamięci,
nic nie jest zapisywane na dysk. Temperatura zewnętrzna i moc palnika pochodzą
z pierwszego kanału z THINGSPEAK_CHANNEL_IDS, a dla dni sprzed jego utworzenia
temperatura jest brana z archiwum Open-Meteo (FUEL_LAT, FUEL_LON).
Kanał ThingSpeak "Pelet":
  field1 = kg dosypane, field2 = notatka, field3 = entry_id anulowanego wpisu

Endpointy (obsługiwane przez fuel.handle(), wywoływane z api.py):
  GET    /api/fuel/stats?days=30
  GET    /api/fuel/daily?days=60   zużycie dobowe z licznika, temperatura, słońce, wiatr, model dobowy
  GET    /api/fuel/refills
  GET    /api/fuel/summary         podsumowanie do karty w dashboardzie
  POST   /api/fuel/refill          {"kg": 30} lub {"bags": 2}, opcjonalnie "ts", "note"   (X-Api-Token)
  DELETE /api/fuel/refill/<id>     anulowanie wpisu                                         (X-Api-Token)

Założenie: przy każdej dosypce zasobnik jest uzupełniany do tego samego poziomu,
więc ilość dosypana = zużycie od poprzedniej dosypki. Pierwsza dosypka to punkt startowy.
"""
from __future__ import annotations

import hmac
import fuel_ai
import fuel_telemetry
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from fuel_schema import field_number, timezone as thermoflow_timezone
from fuel_errors import FuelError
from fuel_http import request_json as _request_json
from fuel_telemetry_provider import fetch_history as fetch_telemetry_history
from fuel_weather_provider import openmeteo_mean as _openmeteo_mean_impl, weather_for_day as _weather_impl
from fuel_refill_provider import fetch_refills as fetch_refill_records, write_refill as write_refill_record
from fuel_thingspeak_daily_provider import fetch_day as fetch_thingspeak_day, detect_data_start as detect_thingspeak_data_start
from fuel_daily_provider import get_daily_raw as get_daily_raw_impl
from fuel_counter_provider import fetch_counter_points
from fuel_estimates import select_usage_estimate
from fuel_summary_metrics import build_refills_based, telemetry_state as build_telemetry_state
from fuel_summary_estimates import build_last7_estimate, build_last_refill_and_hopper
from fuel_house_model import fit_from_refills as fit_house_model_from_refills, active_balance_temperature
from fuel_period_analysis import build_periods as build_refill_periods
from fuel_refills import merge_close_refills as _merge_close_refills_impl, normalize_refill_feeds
from fuel_counter import (
    calibrate_pairs as _calibrate_pairs_impl,
    counter_calibration as _counter_calibration_impl,
    counter_factor as _counter_factor_impl,
    counter_kg as _counter_kg_impl,
    counter_method as _counter_method_impl,
    counter_regime as _counter_regime_impl,
    span_estimate as _span_estimate_impl,
)
from fuel_energy import (
    burner_factor as _burner_factor_impl,
    burner_kg as _burner_kg_impl,
    calibration as _calibration_impl,
    calibrate_burner as _calibrate_burner_impl,
)
from fuel_planning import (
    days_until as _days_until_impl,
    planning_forecast as _planning_forecast_impl,
    rate_for as _rate_for_impl,
)
from fuel_models import (
    LAG_WEIGHT_PREVIOUS,
    LAG_WEIGHT_TODAY,
    _fit_at,
    _hdd_from,
    _lagged_temperature,
    _ols_loo,
    _predict_daily,
    _predict_kg_per_day,
    _solve,
)

TZ = thermoflow_timezone()
API_TOKEN = os.getenv("FUEL_API_TOKEN", "")
TS_API = "https://api.thingspeak.com"

def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [item.strip() for item in raw.split(",")] if raw else []


# Kanał 1 (temperatury i moc) z tych samych zmiennych, których używa api.py.
TS_CHANNEL_TEMPS = (_csv_env("THINGSPEAK_CHANNEL_IDS") or [""])[0]
TS_READ_KEY_TEMPS = (_csv_env("THINGSPEAK_READ_KEYS") or [""])[0]
TS_CHANNEL_VALVE = (_csv_env("THINGSPEAK_CHANNEL_IDS") + ["", ""])[1]
TS_READ_KEY_VALVE = (_csv_env("THINGSPEAK_READ_KEYS") + ["", ""])[1]
FIELD_COUNTER = field_number("fuel_percent", "valve_pumps_fuel")
FIELD_OUTDOOR = field_number("outdoor_c", "temperatures_power")
FIELD_POWER = field_number("boiler_power_kw", "temperatures_power")
# Pusty = wykrywany automatycznie z daty utworzenia kanału.
TS_DATA_START = os.getenv("FUEL_TEMPS_DATA_START", "").strip()

# Osobny kanał na dosypki.
TS_CHANNEL_FUEL = os.getenv("THINGSPEAK_FUEL_CHANNEL_ID", "").strip()
TS_READ_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_READ_KEY", "").strip()
TS_WRITE_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_WRITE_KEY", "").strip()

# Domyślna lokalizacja: Hajnówka, Podlaskie; env może ją nadpisać.
FUEL_LAT = os.getenv("FUEL_LAT", "").strip() or "52.74"
FUEL_LON = os.getenv("FUEL_LON", "").strip() or "23.58"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"

# Temperatura bazowa stopniodni, gdy nie da się jeszcze dopasować modelu budynku (za mało okresów).
HDD_BASE = float(os.getenv("HDD_BASE", "") or 14.0)


def _parse_hdd_base_fixed(raw: str):
    """FUEL_HDD_BASE_FIXED: opcjonalne ręczne wymuszenie temperatury bazowej.

    Puste ustawienie lub „off”/„auto”/„0” oznacza automatyczne dopasowanie z dosypek.
    """
    raw = (raw or "").strip().lower().replace(",", ".")
    if not raw or raw in ("off", "auto", "none", "false", "0"):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if 8.0 <= value <= 22.0 else None


HDD_BASE_FIXED = _parse_hdd_base_fixed(os.getenv("FUEL_HDD_BASE_FIXED", ""))
# Zakres temperatur granicznych sprawdzanych przy dopasowaniu modelu (powyżej nich dom nie potrzebuje ogrzewania).
MODEL_BASES = [value / 2 for value in range(24, 34)]   # 12,0 ... 16,5 °C
MIN_MODEL_PERIODS = 4
OFFSET_MIN_DAYS = 3                                  # tyle pełnych dób z obu źródeł, żeby liczyć korektę
OFFSET_CACHE_SECONDS = 3600
OFFSET_FREEZE_DAYS = 14                              # od tylu dób z obu źródeł korekta Open-Meteo jest zamrożona
OFFSET_RECALC_SECONDS = 7 * 24 * 3600                # zamrożoną korektę przeliczamy raz w tygodniu
FORECAST_CACHE_SECONDS = 3600
PELLET_KWH_PER_KG = float(os.getenv("PELLET_KWH_PER_KG", "4.8"))
BOILER_EFF = float(os.getenv("BOILER_EFF", "0.88"))
BAG_KG = float(os.getenv("PELLET_BAG_KG", "15"))
# Pojemność zasobnika: Gamma 40 wg instrukcji ~228 kg (gęstość nasypowa 0,6 kg/dm3).
HOPPER_KG = float(os.getenv("PELLET_HOPPER_KG", "") or 228)
# Zalecany minimalny poziom wg instrukcji: 15 % objętości. Bez czujnika poziomu pusty zasobnik
# kończy się zgaśnięciem płomienia i alarmem nieudanego rozpalenia, więc ostrzegamy przed rezerwą.
HOPPER_RESERVE_PERCENT = float(os.getenv("PELLET_HOPPER_RESERVE_PERCENT", "") or 15)
SUMMARY_CACHE_SECONDS = 60
SUMMARY_WAIT_SECONDS = 25                # tyle czeka zapytanie przy zimnym starcie (limit nginx to 60 s)

MAX_GAP_S = 300
MAX_DAYS = 120
MIN_PERIOD_DAYS = 2.0
MAX_PERIODS = 50

# Cache storage is grouped in FuelCacheState; TTL/key/invalidation policy remains here.
from fuel_runtime import FuelCacheState

_cache_state = FuelCacheState()
_lock = _cache_state.lock
_daily_cache = _cache_state.daily
_om_cache = _cache_state.openmeteo
_temps_start = _cache_state.temps_start
_refills_cache = _cache_state.refills
_summary_cache = _cache_state.summary
_summary_state = _cache_state.summary_state
_summary_guard = _cache_state.summary_guard
_offset_cache = _cache_state.offset
_forecast_cache = _cache_state.forecast
_climate_cache = _cache_state.climate
CLIMATE_CACHE_SECONDS = 30 * 24 * 3600
CLIMATE_YEARS = 5
_model_cache = _cache_state.house_model
_counter_cache = _cache_state.counter
_weather_cache = _cache_state.weather
_weather_fetched = _cache_state.weather_fetched
_daily_model_cache = _cache_state.daily_model
_telemetry_history_cache = _cache_state.telemetry_history
TELEMETRY_HISTORY_CACHE_SECONDS = 6 * 3600
TELEMETRY_PARTIAL_CACHE_SECONDS = 10 * 60
TELEMETRY_FETCH_CHUNK_DAYS = 3
WEATHER_VARS = "shortwave_radiation_sum,wind_speed_10m_max"
MIN_DAILY_DAYS = 30                    # tyle pełnych dób z licznikiem, żeby dopasować model dobowy
DAILY_MODEL_MIN_GAIN = 0.03            # dodatkowy czynnik musi poprawić błąd prognozy o co najmniej 3 %
MIN_COUNTER_DROP = 20                  # minimalny spadek licznika (pp) do kalibracji
COUNTER_COLLECTING_MESSAGE = "kalibracja licznika po zmianie podajnika: zbieranie danych"
COUNTER_PROVISIONAL_KG_PER_PERCENT = 3.4   # tymczasowo po epoce, do pierwszego oszacowania/kalibracji (szacunek)
COUNTER_ESTIMATE_UNCERTAINTY = 0.10        # niepewność wartości tymczasowej i oszacowania z okresu obejmującego epokę
MIN_SPAN_DROP = 10                     # minimalny spadek licznika po epoce (pp) do oszacowania z okresu obejmującego
REFILL_MERGE_SECONDS = 30 * 60         # wpisy dosypek bliższe niż 30 minut są jedną dosypką
# Oczekiwany współczynnik mocy palnika po zmianie podajnika, do czasu własnej kalibracji.
BURNER_FACTOR_AFTER_EPOCH = 0.94


def _parse_epoch(raw: str) -> datetime:
    """Chwila zmiany wydajności podajnika: od niej licznik ma inny współczynnik kg na 1 %."""
    default = datetime.fromisoformat("2026-11-13T18:27:00+01:00")
    try:
        parsed = datetime.fromisoformat(raw.strip()) if raw.strip() else default
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


COUNTER_EPOCH = _parse_epoch(os.getenv("FUEL_COUNTER_EPOCH", ""))


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _day_bounds(day: date):
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------- temperatury i moc

def _fetch_day(day: date) -> dict:
    return fetch_thingspeak_day(
        day=day, tz=TZ, channel_id=TS_CHANNEL_TEMPS,
        read_key=TS_READ_KEY_TEMPS, endpoint=TS_API,
        outdoor_field=FIELD_OUTDOOR, power_field=FIELD_POWER,
        hdd_base=HDD_BASE, max_gap_seconds=MAX_GAP_S,
        request_json=_request_json, parse_timestamp=_parse_ts,
    )


def _openmeteo_mean(day: date):
    return _openmeteo_mean_impl(
        day=day, latitude=FUEL_LAT, longitude=FUEL_LON, timezone=TZ,
        archive_url=OM_ARCHIVE, forecast_url=OM_FORECAST, cache=_om_cache,
        request_json=_request_json, fuel_error=FuelError,
        now=lambda: datetime.now(TZ),
    )



def _weather(day: date) -> dict:
    """Suma promieniowania słonecznego (MJ/m²) i maks. prędkość wiatru (km/h) dla doby."""
    return _weather_impl(
        day=day, latitude=FUEL_LAT, longitude=FUEL_LON, timezone=TZ,
        archive_url=OM_ARCHIVE, forecast_url=OM_FORECAST, variables=WEATHER_VARS,
        cache=_weather_cache, fetched=_weather_fetched,
        cache_seconds=FORECAST_CACHE_SECONDS, request_json=_request_json,
        fuel_error=FuelError, now=lambda: datetime.now(TZ),
        monotonic=time.monotonic,
    )


def _temps_data_start():
    """Pierwszy dzień z danych ThingSpeak (ustawienie albo data utworzenia kanału)."""
    if TS_DATA_START:
        return date.fromisoformat(TS_DATA_START)
    if not _temps_start:
        start = None
        if TS_CHANNEL_TEMPS:
            try:
                start = detect_thingspeak_data_start(
                    configured_start="", channel_id=TS_CHANNEL_TEMPS,
                    read_key=TS_READ_KEY_TEMPS, endpoint=TS_API,
                    request_json=_request_json, parse_timestamp=_parse_ts,
                    timezone=TZ,
                )
            except FuelError:
                return None   # spróbujemy przy następnym zapytaniu
        _temps_start.append(start)
    return _temps_start[0]


def _get_daily_raw(day: date) -> dict:
    """Doba z preferowanego źródła, z fallbackiem Open-Meteo dla temperatury."""
    return get_daily_raw_impl(
        day=day, today=datetime.now(TZ).date(), cache=_daily_cache,
        channel_configured=bool(TS_CHANNEL_TEMPS),
        data_start=_temps_data_start, fetch_thingspeak=_fetch_day,
        openmeteo_mean=_openmeteo_mean, hdd_base=HDD_BASE,
        fuel_error=FuelError,
    )


def _om_offset() -> dict:
    """Średnia różnica: czujnik sterownika (ThingSpeak) minus Open-Meteo, z dób z obu źródeł."""
    cached = _offset_cache["data"]
    if cached is not None:
        ttl = OFFSET_RECALC_SECONDS if cached.get("frozen") else OFFSET_CACHE_SECONDS
        if time.monotonic() - _offset_cache["at"] < ttl:
            return cached
    diffs = []
    ts_start = _temps_data_start()
    today = datetime.now(TZ).date()
    if ts_start and FUEL_LAT and FUEL_LON:
        day = ts_start
        while day < today:
            try:
                rec = _get_daily_raw(day)
                if rec.get("source") == "thingspeak" and rec.get("t_mean") is not None and rec.get("samples", 0) >= 1000:
                    om = _openmeteo_mean(day)
                    if om is not None:
                        diffs.append(rec["t_mean"] - om)
            except FuelError:
                pass
            day += timedelta(days=1)
    applied = len(diffs) >= OFFSET_MIN_DAYS
    data = {"offset": round(sum(diffs) / len(diffs), 2) if applied else 0.0, "days": len(diffs), "applied": applied,
            "frozen": len(diffs) >= OFFSET_FREEZE_DAYS}
    _offset_cache.update(at=time.monotonic(), data=data)
    return data


def _counter_points(day: date) -> list:
    """Zmiany licznika zapasu sterownika w dobie: [(unix, %), ...]."""
    return fetch_counter_points(
        day=day, today=datetime.now(TZ).date(), cache=_counter_cache,
        channel_id=TS_CHANNEL_VALVE, field_number=FIELD_COUNTER,
        read_key=TS_READ_KEY_VALVE, endpoint=TS_API, tz=TZ,
        request_json=_request_json, parse_timestamp=_parse_ts,
    )


def _counter_drop(start: datetime, end: datetime):
    """Spadek licznika zapasu w przedziale (pp), z pominięciem resetów do 100 %.

    Zwraca (spadek, kompletność danych, czy licznik doszedł do zera).
    """
    ts_start = _temps_data_start()
    if ts_start is None or start.astimezone(TZ).date() < ts_start:
        return None, False, False
    points = []
    d, last = start.astimezone(TZ).date(), end.astimezone(TZ).date()
    while d <= last:
        try:
            points.extend(_counter_points(d))
        except FuelError:
            return None, False, False
        d += timedelta(days=1)
    t0, t1 = start.timestamp(), end.timestamp()
    before = [p for p in points if p[0] <= t0]
    inside = [p for p in points if t0 < p[0] <= t1]
    seq = ([before[-1]] if before else []) + inside
    if len(seq) < 2:
        return None, False, False
    drop = sum(prev[1] - cur[1] for prev, cur in zip(seq, seq[1:]) if cur[1] < prev[1])
    floor_hit = any(value <= 1 for _ts, value in seq)
    return drop, True, floor_hit


def _calibrate_pairs(pairs: list) -> dict:
    return _calibrate_pairs_impl(pairs, _counter_drop, MIN_COUNTER_DROP)


def _fmt_pl(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _span_estimate(refills: list[dict], old: dict):
    return _span_estimate_impl(refills, old, COUNTER_EPOCH, _counter_drop,
                               MIN_SPAN_DROP, COUNTER_ESTIMATE_UNCERTAINTY)


def _counter_calibration(refills: list[dict]) -> dict:
    return _counter_calibration_impl(
        refills, COUNTER_EPOCH, datetime.now(TZ), _counter_drop, MIN_COUNTER_DROP,
        MIN_SPAN_DROP, COUNTER_PROVISIONAL_KG_PER_PERCENT, COUNTER_ESTIMATE_UNCERTAINTY,
        COUNTER_COLLECTING_MESSAGE, _fmt_pl,
    )


def _counter_factor(counter: dict, moment: datetime):
    return _counter_factor_impl(counter, moment, COUNTER_EPOCH)


def _counter_kg(start: datetime, end: datetime, counter: dict):
    return _counter_kg_impl(start, end, counter, COUNTER_EPOCH, _counter_drop)


def _counter_regime(day: date) -> str:
    return _counter_regime_impl(day, _day_bounds, COUNTER_EPOCH)


def _counter_method(counter: dict) -> str:
    return _counter_method_impl(counter, _fmt_pl)


def _counter_day_kg(day: date, counter: dict):
    """Zużycie w pełnej dobie z licznika podajnika (None: doba niepełna, ze zmianą podajnika lub bez kalibracji)."""
    if day >= datetime.now(TZ).date():
        return None
    if _counter_regime(day) == "epoch":
        return None   # doba ze zmianą wydajności podajnika nie ma jednego współczynnika
    start, end = _day_bounds(day)
    factor = _counter_factor(counter, start)
    if not factor:
        return None
    try:
        points = _counter_points(day)
        before = _counter_points(day - timedelta(days=1))
    except FuelError:
        return None
    if not points or not before:
        return None   # brak danych z poprzedniej doby: nie wiadomo, od jakiej wartości startowała
    if points[-1][0] < end.timestamp() - 900:
        return None   # doba urwana (np. przerwa w zapisie)
    drop, complete, floor_hit = _counter_drop(start, end)
    if not complete or floor_hit or drop is None:
        return None
    return round(drop * factor, 1)


def _telemetry_daily_features(days: int) -> tuple[dict, dict]:
    """Return optional telemetry aggregates without allowing telemetry faults to break fuel estimates."""
    try:
        audit = _telemetry_history(min(MAX_DAYS, max(1, days)))
        daily = {row.get("day"): row for row in audit.get("daily", [])}
        fetch_status = audit.get("fetch_status", "complete")
        state = "partial" if fetch_status == "partial" else "available"
        errors = audit.get("fetch_errors") or []
        return daily, {
            "status": state,
            "days": len(daily),
            "fetch_windows": audit.get("fetch_windows"),
            "error": "; ".join(errors)[:180] if errors else None,
            "fetched_at": audit.get("fetched_at"),
        }
    except Exception as error:
        # Telemetry is an optional feature source; preserve counter/refill estimates on any failure.
        return {}, {
            "status": "unavailable",
            "error": f"{type(error).__name__}: {error}"[:180],
        }


def _daily_rows(days: int = 120):
    """Pełne doby z licznikiem podajnika, temperaturą, słońcem i wiatrem."""
    counter = _counter_calibration(_refills())
    ts_start = _temps_data_start()
    today = datetime.now(TZ).date()
    out = []
    if not ts_start:
        return out, counter

    # Telemetria jest dołączana do tych samych dób co skalibrowany licznik paliwa.
    # Wewnętrzna historia modelu może sięgać MAX_DAYS; publiczny audyt ma osobny limit 14 dni.
    telemetry_by_day, telemetry_state = _telemetry_daily_features(days)

    d = max(ts_start + timedelta(days=1), today - timedelta(days=days))
    try:
        prev_t = _get_daily(d - timedelta(days=1)).get("t_mean")
    except FuelError:
        prev_t = None
    base = _active_base()
    while d < today:
        rec = _get_daily(d)
        weather = _weather(d)
        kg = _counter_day_kg(d, counter)
        t_lag = _lagged_temperature(rec.get("t_mean"), prev_t)
        prev_t = rec.get("t_mean")
        telemetry = telemetry_by_day.get(d.isoformat(), {})
        # Do modelu trafiają wyłącznie dobowe agregaty o wystarczającym pokryciu.
        # Analogowe serie wymagają >= 1000 próbek; czasy pomp >= 20 h znanego stanu.
        def analog(name):
            value = telemetry.get(name + "_mean")
            return value if telemetry.get(name + "_samples", 0) >= 1000 else None

        def pump_runtime(name):
            known = telemetry.get(name + "_known_h")
            return telemetry.get(name + "_runtime_h") if known is not None and known >= 20 else None

        out.append({"day": d.isoformat(), "t_mean": rec.get("t_mean"), "hdd": rec.get("hdd"),
                    "t_lag": round(t_lag, 2) if t_lag is not None else None,
                    "hdd_lag": round(max(0.0, base - t_lag), 2) if t_lag is not None else None,
                    "solar_mj": weather.get("solar_mj"), "wind_max_kmh": weather.get("wind_max_kmh"),
                    "telemetry_co_c": analog("co_c"),
                    "telemetry_dhw_c": analog("dhw_c"),
                    "telemetry_floor_c": analog("floor_c"),
                    "telemetry_return_c": analog("return_c"),
                    "telemetry_boiler_power_kw": analog("boiler_power_kw"),
                    "telemetry_valve_opening_pct": analog("valve_opening_pct"),
                    "telemetry_pump_floor_h": pump_runtime("pump_floor"),
                    "telemetry_pump_dhw_h": pump_runtime("pump_dhw"),
                    "telemetry_pump_circulation_h": pump_runtime("pump_circulation"),
                    "telemetry_samples": telemetry.get("co_c_samples", 0),
                    "telemetry_status": telemetry_state["status"],
                    "telemetry_error": telemetry_state.get("error"),
                    "kg_counter": kg, "counter_regime": _counter_regime(d),
                    "estimate": kg is not None and counter["collecting"] and _counter_regime(d) == "new"})
        d += timedelta(days=1)
    return out, counter


def _fit_daily_model(rows: list):
    """Compatibility wrapper for extracted daily model selection."""
    from fuel_daily_model import fit_daily_model
    return fit_daily_model(
        rows, cache=_daily_model_cache, active_base=_active_base,
        min_days=MIN_DAILY_DAYS, lag_weight_today=LAG_WEIGHT_TODAY,
        lag_weight_previous=LAG_WEIGHT_PREVIOUS, min_gain=DAILY_MODEL_MIN_GAIN,
        ols_loo=_ols_loo,
    )

def _active_base() -> float:
    """Temperatura bazowa stopniodni: stała (FUEL_HDD_BASE_FIXED) albo dopasowana do modelu."""
    return active_balance_temperature(
        _model_cache["data"], fixed_base=HDD_BASE_FIXED, fallback_base=HDD_BASE
    )


def _get_daily(day: date, base: float | None = None) -> dict:
    """Doba z korektą Open-Meteo do czujnika sterownika i stopniodniami dla bazy budynku."""
    rec = _get_daily_raw(day)
    if rec.get("source") == "open-meteo" and rec.get("t_mean") is not None:
        offset = _om_offset()
        rec["t_mean_raw"] = rec["t_mean"]
        rec["t_mean"] = round(rec["t_mean"] + offset["offset"], 2)
        rec["om_offset"] = offset["offset"]
    base = _active_base() if base is None else base
    rec["hdd"] = round(max(0.0, base - rec["t_mean"]), 2) if rec.get("t_mean") is not None else None
    rec["hdd_base"] = base
    return rec


def _period_temps(start: datetime, end: datetime):
    """[(udział doby, średnia temperatura)] w przedziale oraz liczba dób bez temperatury."""
    temps, missing = [], 0
    d, last = start.astimezone(TZ).date(), end.astimezone(TZ).date()
    while d <= last:
        day_start, day_end = _day_bounds(d)
        overlap = (min(end, day_end) - max(start, day_start)).total_seconds()
        if overlap > 0:
            rec = _get_daily(d)
            if rec.get("t_mean") is None:
                missing += 1
            else:
                temps.append((overlap / 86400, rec["t_mean"]))
        d += timedelta(days=1)
    return temps, missing


def _fit_model(refills: list[dict]):
    """Model budynku z dosypek: kg/dobę = stała + kg_na_stopień × stopniodni/dobę.

    Domyślnie temperatura bazowa jest dopasowywana do danych. FUEL_HDD_BASE_FIXED
    pozwala opcjonalnie wymusić wartość ręcznie.
    """
    key = (len(refills), refills[-1]["id"] if refills else None, _om_offset()["offset"], HDD_BASE_FIXED)
    if _model_cache["key"] == key:
        return _model_cache["data"]
    model = fit_house_model_from_refills(
        refills, _period_temps,
        min_period_days=MIN_PERIOD_DAYS, max_days=MAX_DAYS,
        min_model_periods=MIN_MODEL_PERIODS, model_bases=MODEL_BASES,
        hdd_base_fixed=HDD_BASE_FIXED, fit_at=_fit_at,
    )
    _model_cache.update(key=key, data=model)
    return model


def _forecast():
    """Prognoza średnich dobowych temperatur (z korektą do czujnika sterownika)."""
    cached = _forecast_cache["data"]
    if cached is not None and time.monotonic() - _forecast_cache["at"] < FORECAST_CACHE_SECONDS:
        return cached
    days = []
    if FUEL_LAT and FUEL_LON:
        try:
            daily = _request_json(OM_FORECAST, {"latitude": FUEL_LAT, "longitude": FUEL_LON,
                                                "daily": "temperature_2m_mean", "timezone": str(TZ),
                                                "forecast_days": 7}).get("daily", {})
            offset = _om_offset()["offset"]
            days = [(date.fromisoformat(t), round(v + offset, 1))
                    for t, v in zip(daily.get("time", []), daily.get("temperature_2m_mean", [])) if v is not None]
            for d, _t in days:
                _weather(d)   # słońce i wiatr do prognozy (w cache po pierwszym zapytaniu)
        except FuelError:
            days = []
    _forecast_cache.update(at=time.monotonic(), data=days)
    return days


def _rate_for(model: dict, daily_model, day: date, t: float, prev_t=None) -> float:
    return _rate_for_impl(model, daily_model, day, t, prev_t, _weather,
                          _predict_daily, _predict_kg_per_day, _lagged_temperature)


def _days_until(kg_needed: float, model: dict, forecast: list, now: datetime,
                daily_model=None, known_temps=None):
    return _days_until_impl(kg_needed, model, forecast, now, daily_model, known_temps,
                            _day_bounds, _rate_for)


def _period_sums(start: datetime, end: datetime, base: float | None = None):
    now = datetime.now(TZ)
    hdd = kwh = 0.0
    hdd_missing = kwh_missing = 0
    d, last = start.astimezone(TZ).date(), end.astimezone(TZ).date()
    while d <= last:
        day_start, day_end = _day_bounds(d)
        overlap = (min(end, day_end) - max(start, day_start)).total_seconds()
        if overlap > 0:
            rec = _get_daily(d, base)
            if rec.get("hdd") is None:
                hdd_missing += 1
            else:
                hdd += rec["hdd"] * overlap / 86400
            if rec.get("burner_kwh") is None:
                kwh_missing += 1
            else:
                elapsed = (min(now, day_end) - day_start).total_seconds()
                kwh += rec["burner_kwh"] * overlap / max(elapsed, 1)
        d += timedelta(days=1)
    return hdd, kwh, hdd_missing, kwh_missing


# ---------------------------------------------------------------- dosypki

def _merge_close_refills(refills: list[dict]) -> list[dict]:
    """Compatibility wrapper for the pure refill event merger."""
    return _merge_close_refills_impl(refills, REFILL_MERGE_SECONDS, _parse_ts)


def _refills() -> list[dict]:
    """Dosypki po scaleniu wpisów dodanych w odstępie krótszym niż 30 minut."""
    return _merge_close_refills(_refills_raw())


def _refills_raw() -> list[dict]:
    return fetch_refill_records(
        channel_id=TS_CHANNEL_FUEL, read_key=TS_READ_KEY_FUEL,
        endpoint=TS_API, request_json=_request_json, cache=_refills_cache,
        ttl_seconds=SUMMARY_CACHE_SECONDS, monotonic=time.monotonic,
        normalize=normalize_refill_feeds, tz=TZ, parse_timestamp=_parse_ts,
    )


def _invalidate_caches():
    _model_cache.update(key=None, data=None)
    _refills_cache.update(at=0.0, data=None)
    _summary_cache.update(at=0.0, data=None)



def _ts_write(fields: dict, created_at: datetime | None = None) -> int:
    return write_refill_record(
        channel_id=TS_CHANNEL_FUEL, write_key=TS_WRITE_KEY_FUEL,
        endpoint=TS_API, request_json=_request_json, fields=fields,
        created_at=created_at, fuel_error=FuelError,
        invalidate=_invalidate_caches, timezone_module=timezone,
    )


def _periods(refills: list[dict]) -> list[dict]:
    """Compatibility wrapper for the extracted refill-period analysis."""
    return build_refill_periods(
        refills,
        period_sums=_period_sums,
        counter_drop=_counter_drop,
        model=_model_cache["data"],
        active_base=_active_base,
        max_periods=MAX_PERIODS,
        max_days=MAX_DAYS,
        pellet_kwh_per_kg=PELLET_KWH_PER_KG,
        boiler_efficiency=BOILER_EFF,
        counter_epoch=COUNTER_EPOCH,
    )

def _calibration(periods: list[dict]) -> dict:
    return _calibration_impl(periods, COUNTER_EPOCH, datetime.now(TZ),
                             BURNER_FACTOR_AFTER_EPOCH, MIN_PERIOD_DAYS)


def _calibrate_burner(usable: list) -> dict:
    return _calibrate_burner_impl(usable, MIN_PERIOD_DAYS)


def _burner_factor(calib: dict, moment: datetime) -> float:
    return _burner_factor_impl(calib, moment, COUNTER_EPOCH)


def _burner_kg(start: datetime, end: datetime, calib: dict):
    return _burner_kg_impl(start, end, calib, COUNTER_EPOCH, _period_sums,
                           PELLET_KWH_PER_KG, BOILER_EFF)


def _stats(query: dict) -> dict:
    try:
        days = max(1, min(int(query.get("days", ["30"])[0]), MAX_DAYS))
    except ValueError:
        raise FuelError(400, "days musi być liczbą") from None

    today = datetime.now(TZ).date()
    errors = []

    def prefetch(d: date):
        try:
            _get_daily_raw(d)
        except FuelError:
            pass

    def safe_daily(d: date) -> dict:
        try:
            return _get_daily(d)
        except FuelError as e:
            return {"day": d.isoformat(), "error": e.message}

    with _lock:
        wanted = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        # pierwsze wywołanie: niezapamiętane doby pobieramy równolegle (limit nginx to 60 s)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(prefetch, wanted))
        try:
            refills = _refills()
            model = _fit_model(refills)      # najpierw model: ustala temperaturę bazową stopniodni
            periods = _periods(refills)
        except FuelError as e:
            refills, periods, model = [], [], None
            errors.append(e.message)
        daily = [safe_daily(d) for d in wanted]

    calib = _calibration(periods)
    for r in daily:
        if r.get("burner_kwh") is None:
            continue
        day_start = _day_bounds(date.fromisoformat(r["day"]))[0]
        est = r["burner_kwh"] / (PELLET_KWH_PER_KG * BOILER_EFF) * _burner_factor(calib, day_start)
        r["est_kg"] = round(est, 1)
        r["est_provisional"] = day_start >= COUNTER_EPOCH and bool(calib["new"].get("provisional"))
        r["est_kg_per_hdd"] = round(est / r["hdd"], 3) if r.get("hdd") else None
        if model and r.get("hdd") and r["hdd"] >= 2:
            r["heating_kg_per_hdd"] = round((est - model["base_kg_per_day"]) / r["hdd"], 2)

    valid = [r for r in daily if r.get("hdd") is not None and "est_kg" in r]
    tot_hdd = sum(r["hdd"] for r in valid)
    tot_kg = sum(r["est_kg"] for r in valid)
    return {
        "model": model,
        "om_offset": _om_offset(),
        "config": {"hdd_base": _active_base(), "hdd_base_default": HDD_BASE, "hdd_base_fixed": HDD_BASE_FIXED, "pellet_kwh_per_kg": PELLET_KWH_PER_KG,
                   "boiler_eff": BOILER_EFF, "bag_kg": BAG_KG},
        "calibration": calib,
        "counter_calibration": _counter_calibration(refills) if refills else None,
        "summary": {"days_with_burner_data": len(valid), "hdd": round(tot_hdd, 1),
                    "est_kg": round(tot_kg, 1),
                    "est_kg_per_hdd": round(tot_kg / tot_hdd, 3) if tot_hdd > 0 else None},
        "refills_count": len(refills),
        "periods": periods,
        "daily": daily,
        "errors": errors,
    }


def _prefetch(days: int = 400):
    """Równolegle pobiera doby z ThingSpeak (temperatury, licznik), żeby zimny start nie trwał minut."""
    if not TS_CHANNEL_TEMPS:
        return
    ts_start = _temps_data_start()   # raz, przed wątkami
    today = datetime.now(TZ).date()
    first = max(ts_start or today - timedelta(days=MAX_DAYS), today - timedelta(days=days))
    wanted = [first + timedelta(days=i) for i in range((today - first).days + 1)]

    def one(day: date):
        for fetch in (_get_daily_raw, _counter_points):
            try:
                fetch(day)
            except FuelError:
                pass   # brak jednej doby nie przerywa rozgrzewania pozostałych

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, wanted))


def _summary_job():
    """Liczy podsumowanie w tle i zapisuje je w cache (błąd zostaje do odczytu przez _summary)."""
    try:
        _prefetch()
        _summary_build()
        _summary_state["error"] = None
    except FuelError as e:
        _summary_state["error"] = e.message
    except Exception as e:   # nieoczekiwany błąd nie może zabić wątku po cichu
        _summary_state["error"] = f"{type(e).__name__}: {e}"[:180]


def _summary_kick():
    with _summary_guard:
        thread = _summary_state["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=_summary_job, name="fuel-summary", daemon=True)
            _summary_state["thread"] = thread
            thread.start()
        return thread


def warmup():
    """Rozgrzewa cache przy starcie kontenera, zanim dashboard poprosi o kartę."""
    if TS_CHANNEL_FUEL:
        _summary_kick()


def _summary() -> dict:
    """Podsumowanie do karty w dashboardzie. Zawsze odpowiada szybko, ciężkie liczenie idzie w tle.

    Ostatni wynik jest zwracany od razu (po 60 s odświeża się w tle). Przy zimnym starcie
    czekamy do SUMMARY_WAIT_SECONDS, a potem zwracamy {"warming": true}, żeby nginx (60 s)
    nie ucinał zapytania, a dashboard ponowił je po chwili.
    """
    if not TS_CHANNEL_FUEL:
        return {"configured": False}
    cached = _summary_cache["data"]
    if cached is not None:
        if time.monotonic() - _summary_cache["at"] >= SUMMARY_CACHE_SECONDS:
            _summary_kick()
        return cached
    thread = _summary_kick()
    thread.join(timeout=SUMMARY_WAIT_SECONDS)
    cached = _summary_cache["data"]
    if cached is not None:
        return cached
    if not thread.is_alive() and _summary_state["error"]:
        raise FuelError(502, _summary_state["error"])
    return {"configured": True, "warming": True}



def _climate_monthly(today: date) -> dict:
    """Pięcioletnie średnie miesięczne dla lokalizacji kotła (Open-Meteo Archive)."""
    cached = _climate_cache["data"]
    if cached and time.monotonic() - _climate_cache["at"] < CLIMATE_CACHE_SECONDS:
        return cached
    end = today - timedelta(days=1)
    start = end.replace(year=end.year - CLIMATE_YEARS)
    params = {
        "latitude": FUEL_LAT, "longitude": FUEL_LON,
        "daily": "temperature_2m_mean,shortwave_radiation_sum,wind_speed_10m_max",
        "timezone": str(TZ), "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    daily = _request_json(OM_ARCHIVE, params).get("daily", {})
    fields = ("temperature_2m_mean", "shortwave_radiation_sum", "wind_speed_10m_max")
    buckets = {}
    for i, day_text in enumerate(daily.get("time", [])):
        bucket = buckets.setdefault(int(day_text[5:7]), {name: [] for name in fields})
        for name in fields:
            values = daily.get(name, [])
            value = values[i] if i < len(values) else None
            if value is not None:
                bucket[name].append(float(value))
    result = {}
    for month, values in buckets.items():
        result[month] = {
            "t_mean": sum(values[fields[0]]) / len(values[fields[0]]) if values[fields[0]] else None,
            "solar_mj": sum(values[fields[1]]) / len(values[fields[1]]) if values[fields[1]] else None,
            "wind_max_kmh": sum(values[fields[2]]) / len(values[fields[2]]) if values[fields[2]] else None,
            "samples": len(values[fields[0]]),
        }
    if not result:
        raise FuelError(502, "Brak danych klimatycznych Open-Meteo dla lokalizacji")
    _climate_cache.update(at=time.monotonic(), data=result)
    return result


def _planning_forecast(model: dict | None, daily_model: dict | None,
                       forecast: list, now: datetime, known_temps: dict) -> dict:
    if not model:
        return {"status": "unavailable", "message": "Za mało danych z dosypek do prognozy"}
    try:
        climate = _climate_monthly(now.date())
    except FuelError as exc:
        return {"status": "unavailable", "message": f"Brak norm pogodowych: {exc.message}"}
    return _planning_forecast_impl(
        model, daily_model, forecast, now, known_temps, climate, _weather, _om_offset,
        _predict_daily, _predict_kg_per_day, _lagged_temperature,
        "Hajnówka, Podlaskie", FUEL_LAT, FUEL_LON, CLIMATE_YEARS,
    )


def _summary_calibration_inputs(refills: list[dict]):
    """Use the same full-history calibration evidence as stats, with a separate recent display window."""
    all_periods = _periods(refills)
    recent_periods = _periods(refills[-7:])
    calibration = _calibration(all_periods)
    counter = _counter_calibration(refills)
    return recent_periods, calibration, counter


def _summary_build() -> dict:
    now = datetime.now(TZ)
    today = now.date()
    with _lock:
        refills = _refills()
        model = _fit_model(refills)                 # model ze wszystkich okresów
        recent_periods, calib, counter = _summary_calibration_inputs(refills)
        last7 = [_get_daily(today - timedelta(days=i)) for i in range(7, 0, -1)]
        offset = _om_offset()
    forecast = _forecast() if model else []
    known_temps = {date.fromisoformat(r["day"]): r["t_mean"] for r in last7 if r.get("t_mean") is not None}
    known_temps.update(dict(forecast))
    try:
        with _lock:
            daily_rows, _counter_info = _daily_rows()
            daily_model = _fit_daily_model(daily_rows)
    except FuelError:
        daily_rows, daily_model = [], None
        ai_shadow = {"status": "unavailable", "message": "Brak danych do analizy"}
    else:
        try:
            ai_shadow = fuel_ai.evaluate(daily_rows)
        except Exception:
            # Eksperyment AI nigdy nie może przerwać ani zmienić obliczeń produkcyjnych.
            ai_shadow = {"status": "unavailable", "message": "Model eksperymentalny chwilowo niedostępny"}

    # 1) ostatnie 7 pełnych dób i 2) zużycie od ostatniej dosypki / zasobnik.
    def estimate_burner_kg(kwh: float, moment: datetime) -> float:
        return kwh / (PELLET_KWH_PER_KG * BOILER_EFF) * _burner_factor(calib, moment)

    def counter_kg_locked(start: datetime, end: datetime, current_counter: dict):
        with _lock:
            return _counter_kg(start, end, current_counter)

    week = build_last7_estimate(
        last7, model, calib, counter, COUNTER_EPOCH, _day_bounds,
        estimate_burner_kg, counter_kg_locked, _predict_kg_per_day,
    )

    # 3) kg na stopniodzień z rzeczywistych dosypek (ostatnie 4 okresy grzewcze)
    refills_based = build_refills_based(recent_periods, model)

    def hopper_period_inputs(start: datetime, end: datetime, current_counter: dict, current_calibration: dict):
        with _lock:
            hdd_value, _kwh, _hm, kwh_missing = _period_sums(start, end)
            counter_value, drop_ok, drop_floor = _counter_kg(start, end, current_counter)
            burner_value = (
                _burner_kg(start, end, current_calibration)
                if not kwh_missing and current_calibration["calibrated"] else None
            )
        return hdd_value, counter_value, drop_ok, drop_floor, burner_value

    last, hopper = build_last_refill_and_hopper(
        now, refills, counter, calib, model, recent_periods, week, forecast,
        daily_model, known_temps, hopper_period_inputs, _counter_method,
        select_usage_estimate, _days_until, HOPPER_KG, HOPPER_RESERVE_PERCENT,
    )

    planning = _planning_forecast(model, daily_model, forecast, now, known_temps)

    telemetry_state = build_telemetry_state(daily_rows)
    result = {
        "configured": True,
        "telemetry": telemetry_state,
        "model": model,
        "hdd_base": _active_base(),
        "hdd_base_fixed": HDD_BASE_FIXED is not None,
        "om_offset": offset,
        "daily_model": daily_model,
        "ai_shadow": ai_shadow,
        "planning": planning,
        "forecast": [{"day": d.isoformat(), "t_mean": t,
                      "solar_mj": _weather(d).get("solar_mj"),
                      "wind_max_kmh": _weather(d).get("wind_max_kmh"),
                      "kg": round(_rate_for(model, daily_model, d, t, known_temps.get(d - timedelta(days=1))), 1)}
                     for d, t in forecast] if model else [],
        "calibration": calib,
        "counter_calibration": counter,
        "last7": week,
        "refills_based": refills_based,
        "last_refill": last,
        "hopper": hopper,
        "fetched_at": now.isoformat(),
    }
    _summary_cache.update(at=time.monotonic(), data=result)
    return result


def _daily(query: dict) -> dict:
    """Dobowe zużycie z licznika obok temperatury, słońca i wiatru oraz stan modelu dobowego."""
    try:
        days = max(1, min(int(query.get("days", ["60"])[0]), MAX_DAYS))
    except ValueError:
        raise FuelError(400, "days musi być liczbą") from None
    _prefetch(days + 2)
    with _lock:
        refills = _refills()
        model = _fit_model(refills)
        rows, counter = _daily_rows(days)
        daily_model = _fit_daily_model(rows)
    for r in rows:
        if model and r["t_mean"] is not None:
            r["model_kg"] = round(_predict_kg_per_day(model, r["t_mean"]), 1)
            if r["kg_counter"] is not None:
                r["residual_kg"] = round(r["kg_counter"] - r["model_kg"], 1)
    telemetry_state = build_telemetry_state(rows)
    return {"model": model, "daily_model": daily_model, "counter_calibration": counter,
            "telemetry": telemetry_state,
            "weather_vars": {"solar_mj": "suma promieniowania krótkofalowego, MJ/m²",
                             "wind_max_kmh": "maksymalna prędkość wiatru na 10 m, km/h"},
            "days": rows}


def _add_refill(data: dict) -> dict:
    try:
        kg = float(data["kg"]) if "kg" in data else float(data["bags"]) * BAG_KG
    except (KeyError, TypeError, ValueError):
        raise FuelError(400, "Podaj 'kg' albo 'bags'") from None
    if not 0 < kg <= 1000:
        raise FuelError(400, "kg poza zakresem")
    dt = None
    if data.get("ts"):
        try:
            dt = datetime.fromisoformat(str(data["ts"]))
        except ValueError:
            raise FuelError(400, "Nieprawidłowy format ts (ISO 8601)") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
    fields = {"field1": kg}
    if data.get("note"):
        fields["field2"] = str(data["note"])[:255]
    entry_id = _ts_write(fields, dt)
    return {"id": entry_id, "ts": (dt or datetime.now(TZ)).isoformat(), "kg": kg}


def _void_refill(raw_id: str) -> dict:
    try:        refill_id = int(raw_id)
    except ValueError:
        raise FuelError(400, "Nieprawidłowe id") from None
    if refill_id not in {r["id"] for r in _refills_raw()}:   # anulujemy pojedynczy wpis, scalenie liczy się od nowa
        raise FuelError(404, "Nie ma takiej dosypki")
    return {"voided": refill_id, "void_entry": _ts_write({"field3": refill_id})}


def _check_token(headers):
    if not API_TOKEN:
        raise FuelError(503, "FUEL_API_TOKEN nie jest ustawiony")
    given = headers.get("X-Api-Token", "").encode("utf-8", "replace")
    if not hmac.compare_digest(given, API_TOKEN.encode("utf-8")):
        raise FuelError(401, "Brak lub zły token")



def _telemetry_fetch_windows(days: int, today: date) -> list[tuple[date, date, int]]:
    """Split inclusive local-day history into bounded, non-overlapping 3-day windows.

    At the configured minimum 35-second ThingSpeak write interval, even a 73-hour
    daylight-saving window remains below the 8,000-feed response limit.
    """
    windows = []
    start_offset = days
    while start_offset >= 0:
        end_offset = max(-1, start_offset - TELEMETRY_FETCH_CHUNK_DAYS)
        start_day = today - timedelta(days=start_offset)
        end_day = today - timedelta(days=end_offset)
        windows.append((start_day, end_day, start_offset))
        start_offset = end_offset
    return windows


def _telemetry_history(days: int) -> dict:
    """Legacy facade: delegate history retrieval to the injectable provider."""
    return fetch_telemetry_history(
        days=days,
        max_days=MAX_DAYS,
        channel_ids=[TS_CHANNEL_TEMPS, TS_CHANNEL_VALVE],
        read_keys=[TS_READ_KEY_TEMPS, TS_READ_KEY_VALVE],
        today=datetime.now(TZ).date(),
        tz=TZ,
        cache=_telemetry_history_cache,
        cache_ttl=TELEMETRY_HISTORY_CACHE_SECONDS,
        partial_cache_ttl=TELEMETRY_PARTIAL_CACHE_SECONDS,
        fetch_windows=_telemetry_fetch_windows,
        request_json=_request_json,
        analyze=fuel_telemetry.analyze,
        endpoint=TS_API,
        monotonic=time.monotonic,
    )


def _telemetry_audit(query: dict) -> dict:
    """Pobiera surową telemetrię dwóch kanałów i zwraca audyt jakości oraz cechy dobowe."""
    try:
        days = max(1, min(int(query.get("days", ["7"])[0]), 14))
    except (TypeError, ValueError):
        raise FuelError(400, "days musi być liczbą") from None
    channel_ids = [TS_CHANNEL_TEMPS, TS_CHANNEL_VALVE]
    read_keys = [TS_READ_KEY_TEMPS, TS_READ_KEY_VALVE]
    if not all(channel_ids):
        raise FuelError(503, "Audyt wymaga dwóch kanałów THINGSPEAK_CHANNEL_IDS")
    feeds_by_channel = [[], []]
    today = datetime.now(TZ).date()
    # Pobieramy osobno każdą dobę: przy próbkowaniu ~40 s mieści się ona
    # bezpiecznie w limicie 8000 rekordów ThingSpeak, bez obcinania historii.
    for channel_index, channel_id in enumerate(channel_ids):
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
            end = start + timedelta(days=1)
            params = {
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "Europe/Warsaw",
                "results": 8000,
            }
            if read_keys[channel_index]:
                params["api_key"] = read_keys[channel_index]
            payload = _request_json(
                f"{TS_API}/channels/{channel_id}/feeds.json", params=params, timeout=12
            )
            feeds_by_channel[channel_index].extend(payload.get("feeds", []))
    result = fuel_telemetry.analyze(feeds_by_channel, channel_ids, days)
    result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return result


def handle(method: str, path: str, query: dict, headers, body: bytes):
    """Zwraca (status, payload) dla ścieżek /api/fuel/..., w innym przypadku None."""
    if not path.startswith("/api/fuel/"):
        return None
    if path != "/api/fuel/" and path.endswith("/"):
        path = path.rstrip("/")
    try:
        if method == "GET" and path == "/api/fuel/stats":
            return 200, _stats(query)
        if method == "GET" and path.rstrip("/") == "/api/fuel/summary":
            return 200, _summary()
        if method == "GET" and path == "/api/fuel/daily":
            return 200, _daily(query)
        if method == "GET" and path == "/api/fuel/telemetry-audit":
            return 200, _telemetry_audit(query)
        if method == "GET" and path == "/api/fuel/refills":
            return 200, {"refills": _refills()}
        if method == "POST" and path == "/api/fuel/refill":
            _check_token(headers)
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise FuelError(400, "Treść musi być poprawnym JSON") from None
            return 201, _add_refill(data if isinstance(data, dict) else {})
        if method == "DELETE" and path.startswith("/api/fuel/refill/"):
            _check_token(headers)
            return 200, _void_refill(path.rsplit("/", 1)[1])
        if method == "GET" and path == "/api/fuel/refill":
            return 405, {"error": "Dosypki dodaje się metodą POST; listę zwraca /api/fuel/refills"}
        return 404, {"error": "Nie znaleziono"}
    except FuelError as e:
        return e.status, {"error": e.message}