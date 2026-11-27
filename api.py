"""Read-only TECH/eModul data bridge for the ThermoFlow dashboard."""

from __future__ import annotations

import base64
import binascii
import json
import hmac
import re
from bisect import bisect_right
import os
import threading
import time
import unicodedata
from datetime import datetime, timezone
from fuel_schema import timezone as thermoflow_timezone
from thermoflow_shadow_runtime import PATH as SHADOW_PATH, ShadowRuntime
from thermoflow_unified_runtime import PATH as INTELLIGENCE_PATH, REFRESH_SECONDS, UnifiedRuntime
from thermoflow_live_ml import LiveMLRuntime
from thermoflow_emodul_store import persist_emodul_snapshot
from thermoflow_production_acceptance import PATH as ACCEPTANCE_PATH, build_acceptance_report
from thermoflow_acceptance_evidence import build_acceptance_evidence
from thermoflow_model_monitoring import PATH as MODEL_MONITORING_PATH, monitor_sqlite_database
from thermoflow_sqlite_data_audit import read_sqlite_data_audit
from thermoflow_weather_forecast import project_weather_forecast
from fibaro_hc3_provider import HC3Config, HC3Error, FibaroHC3Provider
from fibaro_hc3_api import build_hc3_snapshot
from thermoflow_fibaro_diagnostics import build_fibaro_zone_diagnostics
from thermoflow_fibaro_telemetry import build_fibaro_telemetry
from thermoflow_fibaro_event_history import analyze_fibaro_event_history, MAX_HISTORY_SECONDS
from thermoflow_fibaro_store_api import read_fibaro_store_status, read_fibaro_store_history, read_fibaro_store_shadow
from thermoflow_fibaro_dashboard import read_fibaro_dashboard
from thermoflow_fibaro_comfort import read_fibaro_comfort

# Short in-memory cache (no disk writes): the projection parses stored snapshots.
_COMFORT_CACHE = {}
_COMFORT_CACHE_SECONDS = 30
from thermoflow_heating_diagnostics import read_heating_analytics
from thermoflow_analytics_scheduler import start_background_scheduler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fuel_schema import (
    VALVE_OPENING_SERIES_ID,
    VALVE_OPENING_SERIES_LABEL,
    VALVE_SETPOINT_SERIES_ID,
    VALVE_SETPOINT_SERIES_LABEL,
    api_channel_fields,
)


API_BASE = "https://emodul.eu/api/v1/"
POLL_CACHE_SECONDS = 20
STATISTICS_CACHE_SECONDS = 30
HISTORY_CACHE_SECONDS = 30
BUFFER_UPPER_SERIES_ID = "1856"  # eModul: "Temperatura bufora górna" (C1)
# Stable Polish text IDs used by this controller's standard telemetry tiles.
# Keep these as a fallback: a transient failure of /i18n/pl must not make live
# temperatures and relay states disappear from the dashboard.
FALLBACK_LABELS = {
    "550": "Podajnik",
    "571": "Pompa CO",
    "573": "Pompa CWU",
    "575": "Pompa cyrkulacyjna",
    "789": "Temperatura pokoju bieżąca",
    "587": "Pompy równoległe",
    "791": "Temperatura spalin",
    "795": "Temperatura zewnętrzna",
    "814": "Tryb pracy",
    "233": "Czuwanie",
    "1039": "Stan sterownika",
    "1041": "Temperatura C.W.U.",
    "1042": "Grzałka",
    "1050": "Temperatura CO",
    "1666": "Pokój dogrzany",
    "1667": "CWU dogrzane",
}
STATISTICS_LABELS = {
    "789": "Temperatura pokoju bieżąca",
    "791": "Temperatura spalin",
    "795": "Temperatura zewnętrzna",
    "992": "Zawór wbudowany 1",
    "1041": "Temperatura C.W.U.",
    "1050": "Temperatura CO",
    "1856": "Temperatura podłogówki (C1)",
    "3159": "Moc chwilowa",
}
_cache: dict = {"updated": 0.0, "payload": None}
_cache_lock = threading.Lock()
_shadow_runtime = ShadowRuntime()
_intelligence_bundle = os.getenv("THERMOFLOW_INTELLIGENCE_BUNDLE", "").strip()
_intelligence_runtime = UnifiedRuntime(_intelligence_bundle)
_live_ml_runtime = LiveMLRuntime(
    burner_path=os.getenv("THERMOFLOW_BURNER_MODEL_PATH", ""),
    fuel_path=os.getenv("THERMOFLOW_FUEL_MODEL_PATH", ""),
    db_path=os.getenv("THERMOFLOW_DB_PATH", ""),
)
_statistics_cache: dict[str, tuple[float, float | None, str | None, float | None, str | None, dict]] = {}
_statistics_lock = threading.Lock()
_history_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_history_cache_lock = threading.Lock()
_chart_cache: dict[tuple[str, str, int, int], tuple[float, dict]] = {}
_chart_cache_lock = threading.Lock()
_i18n_cache: dict[str, str] | None = None
THINGSPEAK_COMPACT_CHANNEL_FIELDS = api_channel_fields()

THINGSPEAK_LAST_WRITES = [0.0] * len(THINGSPEAK_COMPACT_CHANNEL_FIELDS)
THINGSPEAK_WRITE_LOCK = threading.Lock()
THINGSPEAK_FIRE_LAST_WRITE = 0.0
THINGSPEAK_WRITE_INTERVAL_SECONDS = max(0, int(os.getenv("THINGSPEAK_WRITE_INTERVAL_SECONDS", "40")))


def _thingspeak_field_schema() -> tuple:
    return THINGSPEAK_COMPACT_CHANNEL_FIELDS


def _token() -> str:
    token_file = os.getenv("TECH_API_TOKEN_FILE")
    if token_file:
        with open(token_file, encoding="utf-8") as stream:
            return stream.read().strip()
    return os.getenv("TECH_API_TOKEN", "").strip()


def _thingspeak_settings(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [item.strip() for item in raw.split(",")] if raw else []


def _nested_value(value: dict, path: str):
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _thingspeak_write(payload: dict) -> None:
    """Send one snapshot to each configured ThingSpeak channel, without disk buffering."""
    if os.getenv("THINGSPEAK_WRITE_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    channel_ids = _thingspeak_settings("THINGSPEAK_CHANNEL_IDS")
    write_keys = _thingspeak_settings("THINGSPEAK_WRITE_KEYS")
    fields_by_channel = _thingspeak_field_schema()
    if len(channel_ids) != len(fields_by_channel) or len(write_keys) != len(fields_by_channel):
        return
    now = time.monotonic()
    data = payload.get("data") or {}
    try:
        created_at = datetime.fromisoformat(payload["fetched_at"]).astimezone(timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError):
        created_at = datetime.now(timezone.utc).isoformat()
    with THINGSPEAK_WRITE_LOCK:
        for index, (channel_id, write_key, fields) in enumerate(zip(channel_ids, write_keys, fields_by_channel)):
            if THINGSPEAK_LAST_WRITES[index] and now - THINGSPEAK_LAST_WRITES[index] < THINGSPEAK_WRITE_INTERVAL_SECONDS:
                continue
            form = {"api_key": write_key, "created_at": created_at}
            for field_number, (_series_id, _label, path) in enumerate(fields, start=1):
                if path is None:
                    continue
                value = _nested_value(data, path)
                if isinstance(value, bool):
                    value = int(value)
                value = _number(value)
                if value is not None:
                    form[f"field{field_number}"] = str(value)
            if len(form) == 2:
                continue
            request = Request(
                f"https://api.thingspeak.com/update.json?{urlencode({'api_key': write_key})}",
                data=urlencode({key: value for key, value in form.items() if key != "api_key"}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=4) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result and result.get("entry_id"):
                    THINGSPEAK_LAST_WRITES[index] = now
            except Exception:
                # Live telemetry stays usable when remote history is unreachable.
                continue


def _thingspeak_write_fire(payload: dict) -> None:
    """Write the eModul flame sensor snapshot to its optional dedicated channel."""
    global THINGSPEAK_FIRE_LAST_WRITE
    channel_id = os.getenv("THINGSPEAK_FIRE_CHANNEL_ID", "").strip()
    write_key = os.getenv("THINGSPEAK_FIRE_WRITE_KEY", "").strip()
    if not channel_id or not write_key:
        return
    if os.getenv("THINGSPEAK_WRITE_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return

    data = payload.get("data") or {}
    raw_fire = data.get("fire")
    if isinstance(raw_fire, bool):
        fire_detected = int(raw_fire)
    elif isinstance(raw_fire, (int, float)) and not isinstance(raw_fire, bool):
        fire_detected = int(raw_fire >= 0.5)
    else:
        fire_detected = None
    brightness = _number(data.get("fire_brightness"))
    if fire_detected is None and brightness is None:
        return

    now = time.monotonic()
    with THINGSPEAK_WRITE_LOCK:
        if THINGSPEAK_FIRE_LAST_WRITE and now - THINGSPEAK_FIRE_LAST_WRITE < THINGSPEAK_WRITE_INTERVAL_SECONDS:
            return
        try:
            created_at = datetime.fromisoformat(payload["fetched_at"]).astimezone(timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError):
            created_at = datetime.now(timezone.utc).isoformat()
        form = {"api_key": write_key, "created_at": created_at}
        if fire_detected is not None:
            form["field1"] = str(fire_detected)
        if brightness is not None:
            form["field2"] = str(brightness)
        request = Request(
            f"https://api.thingspeak.com/update.json?{urlencode({'api_key': write_key})}",
            data=urlencode({key: value for key, value in form.items() if key != "api_key"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=4) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result and result.get("entry_id"):
                THINGSPEAK_FIRE_LAST_WRITE = now
        except Exception:
            # A flame-channel outage must never interrupt live eModul telemetry.
            return


def _thingspeak_history(period: str, year: int, month: int) -> tuple[list[dict], list[dict]]:
    """Read configured channel feeds for a local calendar month/year."""
    channel_ids = _thingspeak_settings("THINGSPEAK_CHANNEL_IDS")
    read_keys = _thingspeak_settings("THINGSPEAK_READ_KEYS")
    fields_by_channel = _thingspeak_field_schema()
    if len(channel_ids) != len(fields_by_channel):
        return [], []
    local_tz = thermoflow_timezone()
    start = datetime(year, 1 if period == "year" else month, 1, tzinfo=local_tz)
    if period == "year" or month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=local_tz)
    timescale = 10 if period == "month" else 240
    series_by_id: dict[str, dict] = {}
    pump_states_by_time: dict[str, dict] = {}
    for channel_index, channel_id in enumerate(channel_ids):
        # ThingSpeak's feeds API expects local wall-clock timestamps in
        # YYYY-MM-DD HH:MM:SS format when `timezone` is provided. ISO strings
        # with a T and UTC offset can be silently interpreted as no matches.
        params = {
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Europe/Warsaw",
            "timescale": timescale,
            "results": 8000,
        }
        if channel_index < len(read_keys) and read_keys[channel_index]:
            params["api_key"] = read_keys[channel_index]
        request = Request(f"https://api.thingspeak.com/channels/{channel_id}/feeds.json?{urlencode(params)}", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=8) as response:
                feeds = json.loads(response.read().decode("utf-8")).get("feeds", [])
        except HTTPError as error:
            raise RuntimeError(
                f"ThingSpeak odrzucił odczyt kanału {channel_id} (HTTP {error.code}); sprawdź klucz Read API i prywatność kanału."
            ) from None
        except Exception:
            raise RuntimeError(f"Nie udało się pobrać historii ThingSpeak z kanału {channel_id}.") from None
        for feed in feeds:
            timestamp = feed.get("created_at")
            if not timestamp:
                continue
            for field_number, (series_id, label, field_path) in enumerate(fields_by_channel[channel_index], start=1):
                if series_id is None:
                    continue
                raw = feed.get(f"field{field_number}")
                value = _number(raw)
                if value is None:
                    continue
                item = series_by_id.setdefault(series_id, {"id": series_id, "label": label, "points": []})
                item["points"].append({"x": timestamp, "y": value})
                if field_path and field_path.startswith("pumps."):
                    pump_key = field_path.split(".")[-1]
                    pump_sample = pump_states_by_time.setdefault(timestamp, {"x": timestamp, "pumps": {}})
                    pump_sample["pumps"][pump_key] = value >= 0.5
    return list(series_by_id.values()), sorted(pump_states_by_time.values(), key=lambda item: item["x"])


def _user_id(token: str) -> str:
    """Read the user id claim; the API still validates the signed bearer token."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return str(claims["user_id"])
    except (IndexError, KeyError, ValueError, binascii.Error, json.JSONDecodeError):
        raise RuntimeError("Token eModul nie zawiera poprawnego user_id") from None


def _get(path: str, token: str):
    request = Request(
        API_BASE + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def _translations(token: str) -> dict[str, str]:
    global _i18n_cache
    if _i18n_cache is None:
        try:
            body = _get("i18n/pl", token)
            translated = {str(k): str(v) for k, v in body.get("data", {}).items()}
            if translated:
                _i18n_cache = translated
                return _i18n_cache
        except Exception:
            pass
    return _i18n_cache or {}


def _number(value, divisor: float = 1):
    try:
        return round(float(value) / divisor, 1)
    except (TypeError, ValueError):
        return None


def _label(params: dict, translations: dict[str, str], key: str = "txtId") -> str:
    text_id = str(params.get(key, ""))
    return FALLBACK_LABELS.get(text_id) or translations.get(text_id, "")


def _capture_controller_metric(data: dict, label: str, value, at: str | None = None, unit=None) -> None:
    normalized = _normal_text(label)
    metric = None
    divisor = 1
    try:
        unit_code = int(unit)
    except (TypeError, ValueError):
        unit_code = None
    if "podajnik" in normalized and ("temp" in normalized or "temperatur" in normalized):
        metric, divisor = "feeder_temperature_c", {6: 1, 7: 10}.get(unit_code, 10)
    elif "zuzycie paliwa" in normalized and ("chwil" in normalized or "aktual" in normalized):
        metric = "instant_fuel_consumption"
        divisor = {22: 10, 30: 100}.get(unit_code, 1)
    if metric and value is not None:
        unit_label = ({22: "L/min", 30: "L/h"}.get(unit_code) if metric == "instant_fuel_consumption" else "°C")
        data["controller_metrics"][metric] = {
            "value": _number(value, divisor), "unit": unit_label, "at": at, "label": label
        }


def _normal_text(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFD", value.casefold())
        if unicodedata.category(char) != "Mn"
    )


def _live_statistics(udid: str, token: str) -> tuple[float | None, str | None, float | None, str | None, dict]:
    """Read known live series and discover feeder/fuel metrics by their API labels."""
    now = time.monotonic()
    with _statistics_lock:
        cached = _statistics_cache.get(udid)
        if cached and now - cached[0] < STATISTICS_CACHE_SECONDS:
            return cached[1], cached[2], cached[3], cached[4], cached[5]

    values = {BUFFER_UPPER_SERIES_ID: (None, None), "3159": (None, None)}
    discovered: dict = {}
    try:
        body = _get(f"modules/{udid}/statistics/linear/range/day", token)
        stats_data = body.get("data") or {}
        history = stats_data.get("history") or {}
        raw_descriptions = stats_data.get("descriptions") or []
        if isinstance(raw_descriptions, list):
            descriptions = {str(key): str(raw_descriptions[index]) for index, key in enumerate(history) if index < len(raw_descriptions)}
        elif isinstance(raw_descriptions, dict):
            descriptions = {str(key): str(value) for key, value in raw_descriptions.items()}
        else:
            descriptions = {}
        translations = _translations(token)
        for series_id in values:
            points = history.get(series_id) or []
            points = [point for point in points if isinstance(point, dict) and point.get("y") is not None]
            if points:
                latest = max(points, key=lambda point: str(point.get("x", "")))
                values[series_id] = (_number(latest.get("y")), str(latest.get("x", "")) or None)
        for series_id, points in history.items():
            if not isinstance(points, list):
                continue
            description = descriptions.get(str(series_id), str(series_id))
            label = description
            match = re.fullmatch(r"(\d+)(?::(\d+))?", description)
            if match:
                label = STATISTICS_LABELS.get(match.group(1)) or translations.get(match.group(1), STATISTICS_LABELS.get(str(series_id), ""))
            normalized = _normal_text(label)
            metric = "feeder_temperature_c" if "podajnik" in normalized and ("temp" in normalized or "temperatur" in normalized) else None
            if "zuzycie paliwa" in normalized and ("chwil" in normalized or "aktual" in normalized):
                metric = "instant_fuel_consumption"
            if metric:
                usable = [point for point in points if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None]
                if usable:
                    latest = max(usable, key=lambda point: str(point.get("x", "")))
                    discovered[metric] = {"value": _number(latest["y"]), "at": str(latest["x"]), "label": label}
    except Exception:
        # Statistics are supplementary; a chart endpoint failure must not
        # interrupt the ordinary live telemetry.
        pass

    with _statistics_lock:
        _statistics_cache[udid] = (now, *values[BUFFER_UPPER_SERIES_ID], *values["3159"], discovered)
    return *values[BUFFER_UPPER_SERIES_ID], *values["3159"], discovered


def _linear_statistics(udid: str, token: str, period: str, year: int, month: int) -> dict:
    """Read chart history exclusively from the configured ThingSpeak channels."""
    now = time.monotonic()
    # day/week/month są pobierane dla całego miesiąca, year dla całego roku.
    cache_key = ("thingspeak", period, year, 0 if period == "year" else month)
    with _chart_cache_lock:
        cached = _chart_cache.get(cache_key)
        if cached and now - cached[0] < 300:
            return cached[1]

    series, pump_states = _thingspeak_history(period, year, month)
    result = {
        "configured": True,
        "connected": True,
        "period": period,
        "year": year,
        "month": month if period == "month" else None,
        "history_source": "thingspeak",
        "series": series,
        "pump_states": pump_states,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    # Do not keep an empty first read cached: the channel may have just
    # received its first samples, or a transiently empty feed may recover.
    if series or pump_states:
        with _chart_cache_lock:
            _chart_cache[cache_key] = (now, result)
    return result


def _read_statistics(period: str, year: int | None = None, month: int | None = None) -> dict:
    if period not in ("day", "week", "month", "year"):
        return {"configured": False, "connected": False, "error": "Nieobsługiwany zakres statystyk"}
    today = datetime.now()
    selected_year = year or today.year
    selected_month = month or today.month
    if not 2000 <= selected_year <= 2100 or not 1 <= selected_month <= 12:
        return {"configured": False, "connected": False, "error": "Nieprawidłowa data statystyk"}
    if len(_thingspeak_settings("THINGSPEAK_CHANNEL_IDS")) != len(THINGSPEAK_COMPACT_CHANNEL_FIELDS):
        return {
            "configured": False,
            "connected": False,
            "error": "Historia wymaga dwóch kanałów ThingSpeak: temperatury i mocy oraz zaworu, pomp i paliwa.",
        }
    return _linear_statistics("", "", period, selected_year, selected_month)


def _history_timestamp(value) -> datetime | None:
    """Parse eModul's local YYYYMMDDHHMM sample timestamps."""
    raw = str(value or "")
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=thermoflow_timezone())
    except ValueError:
        return None


HISTORY_SERIES_ALIASES = {
    "thermoflow-valve4d": {
        "thermoflow-valve4d-current", "thermoflow-valve4d-effective-setpoint",
        "thermoflow-valve4d-opening", "thermoflow-valve4d-return",
    },
    "valve-opening": {"thermoflow-valve4d-opening"},
    "opening": {"thermoflow-valve4d-opening"},
    "return": {"thermoflow-valve4d-return"},
    "setpoint": {"thermoflow-valve4d-effective-setpoint"},
    "valve-setpoint": {"thermoflow-valve4d-effective-setpoint"},
}
HISTORY_SERIES_ORDER = (
    "thermoflow-valve4d-current",
    "thermoflow-valve4d-opening",
    "thermoflow-valve4d-return",
    "thermoflow-valve4d-effective-setpoint",
    "thermoflow-outdoor",
    "thermoflow-room",
    "thermoflow-co",
    "thermoflow-boiler-power",
    "thermoflow-flue",
    "thermoflow-floor",
    "thermoflow-dhw",
    "thermoflow-pump-valve4d",
    "thermoflow-pump-floor",
    "thermoflow-pump-dhw",
    "thermoflow-pump-circulation",
    "thermoflow-fuel-percent",
)
# Otwarcie zaworu po ochronie powrotu nie schodzi niżej niż minimalne otwarcie (menu 2203).
HISTORY_PROTECTION_MIN_OPENING = 35
HISTORY_COLD_GAP_MARGIN = 5.0


def _value_at(points: list, timestamp: int):
    """Ostatnia wartość serii w chwili timestamp (seria schodkowa)."""
    index = bisect_right([point[0] for point in points], timestamp) - 1
    return points[index][1] if index >= 0 else None


def _compress_changes(points: list) -> list:
    """Bezstratnie: punkt tylko przy zmianie wartości, plus ostatnia próbka."""
    if not points:
        return []
    compact = [points[0]]
    for point in points[1:]:
        if point[1] != compact[-1][1]:
            compact.append(point)
    if compact[-1][0] != points[-1][0]:
        compact.append(points[-1])
    return compact


def _intervals(points: list) -> list:
    """Przedziały, w których seria 0/1 miała wartość 1: [[start, koniec], ...]."""
    result, opened = [], None
    for timestamp, value in points:
        if value and opened is None:
            opened = timestamp
        elif not value and opened is not None:
            result.append([opened, timestamp])
            opened = None
    if opened is not None and points:
        result.append([opened, points[-1][0]])
    return result


def _local_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, thermoflow_timezone()).strftime("%Y-%m-%d %H:%M")


def _history_analysis(raw: dict, minimum_opening: float | None = None) -> dict:
    """Cykle palnika i ochrony powrotu wyliczone z próbek (to, co wcześniej liczone było ręcznie)."""
    # Zachowaj dotychczasowy próg dla klientów API, które nie przekazują konfiguracji.
    # Dashboard podaje aktualną nastawę 2203 odczytaną z eModul.
    try:
        protection_minimum = float(minimum_opening)
        if not 0 <= protection_minimum <= 100:
            raise ValueError
    except (TypeError, ValueError):
        protection_minimum = float(HISTORY_PROTECTION_MIN_OPENING)
    power = raw.get("thermoflow-boiler-power", [])
    burner = []
    run_start = peak = None
    for timestamp, value in power:
        if value > 0 and run_start is None:
            run_start, peak = timestamp, value
        elif value > 0:
            peak = max(peak, value)
        elif run_start is not None:
            burner.append({"start": _local_iso(run_start), "end": _local_iso(timestamp),
                           "minutes": round((timestamp - run_start) / 60, 1), "peak_kw": peak})
            run_start = None
    if run_start is not None and power:
        burner.append({"start": _local_iso(run_start), "end": None, "running": True,
                       "minutes": round((power[-1][0] - run_start) / 60, 1), "peak_kw": peak})

    opening = raw.get("thermoflow-valve4d-opening", [])
    valve = raw.get("thermoflow-valve4d-current", [])
    setpoint = raw.get("thermoflow-valve4d-effective-setpoint", [])
    returns = raw.get("thermoflow-valve4d-return", [])
    floor = raw.get("thermoflow-pump-floor", [])
    valve_pump = raw.get("thermoflow-pump-valve4d", [])
    cycles = []
    index = 1
    while index < len(opening):
        before, now = opening[index - 1][1], opening[index][1]
        if not (before >= 70 and now <= before - 15):
            index += 1
            continue
        start = opening[index][0]
        # dojście do minimum
        low = index
        while low < len(opening) and opening[low][1] > protection_minimum:
            low += 1
        if low >= len(opening):
            break
        floor_level = opening[low][1]
        # nie ochrona powrotu: pompa zaworu stała albo zawór zjechał wyraźnie poniżej minimum (np. przerwa w pracy sterownika)
        pump_on = _value_at(valve_pump, opening[low][0]) if valve_pump else 1
        if not pump_on or floor_level < protection_minimum - 15:
            index = low + 1
            continue
        rise = low
        while rise < len(opening) and opening[rise][1] <= floor_level + 1:
            rise += 1
        ramp = rise
        while ramp < len(opening) and opening[ramp][1] < 80:
            ramp += 1
        gap_end = None
        for timestamp, value in valve:
            if timestamp <= start:
                continue
            target = _value_at(setpoint, timestamp)
            if target is not None and value >= target - HISTORY_COLD_GAP_MARGIN:
                gap_end = timestamp
                break
        window_end = gap_end or (opening[-1][0])
        gap_returns = [value for timestamp, value in returns if start <= timestamp <= window_end]
        gap_valve = [value for timestamp, value in valve if start <= timestamp <= window_end]
        cycles.append({
            "protection_start": _local_iso(start),
            "floor_pump_on": bool(_value_at(floor, start)) if floor else None,
            "min_opening": floor_level,
            "wait_at_min_minutes": round((opening[rise][0] - opening[low][0]) / 60, 1) if rise < len(opening) else None,
            "ramp_to_80_minutes": round((opening[ramp][0] - opening[rise][0]) / 60, 1) if ramp < len(opening) and rise < len(opening) else None,
            "cold_gap_minutes": round((gap_end - start) / 60, 1) if gap_end else None,
            "cold_gap_definition": f"do 4D >= zadana - {HISTORY_COLD_GAP_MARGIN:g} K",
            "min_return": min(gap_returns) if gap_returns else None,
            "min_valve4d": min(gap_valve) if gap_valve else None,
            "complete": gap_end is not None,
        })
        # następna ochrona dopiero po powrocie zaworu powyżej 70 %
        index = max(ramp, low + 1)

    def mean(values):
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 1) if values else None

    done = [cycle for cycle in cycles if cycle["complete"]]
    return {
        "burner_runs": burner,
        "burner_starts": len([run for run in burner]),
        "protection_cycles": cycles,
        "summary": {
            "cycles": len(cycles),
            "cold_gap_mean_floor_on": mean([c["cold_gap_minutes"] for c in done if c["floor_pump_on"]]),
            "cold_gap_mean_floor_off": mean([c["cold_gap_minutes"] for c in done if c["floor_pump_on"] is False]),
            "min_return": min([c["min_return"] for c in cycles if c["min_return"] is not None], default=None),
            "burner_minutes_total": round(sum(run["minutes"] for run in burner), 1),
        },
    }


def _history_query_datetime(value: str) -> datetime:
    # Ogranicz wejście przed parsowaniem; parametr pochodzi bezpośrednio z URL.
    if not isinstance(value, str):
        raise ValueError("Datę podaj w formacie ISO, np. 2026-11-08T12:00:00+01:00")
    value = value.strip()
    if not value or len(value) > 64:
        raise ValueError("Datę podaj w formacie ISO, np. 2026-11-08T12:00:00+01:00")

    # Niezakodowany "+" w query stringu jest przez parse_qs zamieniany na spację.
    # Odtwórz wyłącznie poprawny, krótki offset; bez wyrażeń regularnych.
    prefix, separator, offset = value.rpartition(" ")
    valid_offset = (
        len(offset) == 5
        and offset[2] == ":"
        and offset[:2].isdigit()
        and offset[3:].isdigit()
    ) or (len(offset) == 4 and offset.isdigit())
    if separator and valid_offset:
        value = f"{prefix}+{offset}"

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Datę podaj w formacie ISO, np. 2026-11-08T12:00:00+01:00") from None
    return parsed.replace(tzinfo=thermoflow_timezone()) if parsed.tzinfo is None else parsed

def _read_thermoflow_fire_history(start: datetime, end: datetime, local_tz) -> dict[str, list]:
    """Fetch the dedicated FIRE channel for ThermoFlow diagnostics.

    FIRE is intentionally optional: its outage must not break the primary
    temperature/valve history or any existing fuel audit endpoint.
    """
    channel_id = os.getenv("THINGSPEAK_FIRE_CHANNEL_ID", "").strip()
    read_key = os.getenv("THINGSPEAK_FIRE_READ_KEY", "").strip()
    if not channel_id:
        return {}
    params = {
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Europe/Warsaw",
        "results": 8000,
    }
    if read_key:
        params["api_key"] = read_key
    request = Request(
        f"https://api.thingspeak.com/channels/{channel_id}/feeds.json?{urlencode(params)}",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            feeds = json.loads(response.read().decode("utf-8")).get("feeds", [])
    except Exception:
        # Optional FIRE telemetry must never make the core history unavailable.
        return {}
    fire_points, brightness_points = [], []
    for feed in feeds:
        raw_timestamp = feed.get("created_at")
        if not raw_timestamp:
            continue
        try:
            timestamp = datetime.fromisoformat(
                str(raw_timestamp).replace("Z", "+00:00")
            ).astimezone(local_tz)
        except ValueError:
            continue
        if timestamp < start or timestamp > end:
            continue
        unix_ts = int(timestamp.timestamp())
        detected = _number(feed.get("field1"))
        brightness = _number(feed.get("field2"))
        if detected is not None:
            fire_points.append([unix_ts, detected])
        if brightness is not None:
            brightness_points.append([unix_ts, brightness])
    result = {}
    if fire_points:
        result["thermoflow-fire"] = sorted(fire_points)
    if brightness_points:
        result["thermoflow-fire-brightness"] = sorted(brightness_points)
    return result


def _read_history(hours: int, requested_series: set[str] | None = None, requested_start: datetime | None = None, requested_end: datetime | None = None, step: int = 0, output_format: str = "compact", with_analysis: bool = True, minimum_opening: float | None = None) -> dict:
    """Return recent ThingSpeak samples as compact [unix_seconds, value] pairs."""
    channel_ids = _thingspeak_settings("THINGSPEAK_CHANNEL_IDS")
    read_keys = _thingspeak_settings("THINGSPEAK_READ_KEYS")
    fields_by_channel = _thingspeak_field_schema()
    if len(channel_ids) != len(fields_by_channel):
        return {"configured": False, "connected": False, "error": "Historia wymaga dwóch kanałów ThingSpeak"}

    local_tz = thermoflow_timezone()
    end = requested_end.astimezone(local_tz) if requested_end else datetime.now(local_tz)
    start = requested_start.astimezone(local_tz) if requested_start else datetime.fromtimestamp(end.timestamp() - hours * 3600, local_tz)
    if start >= end:
        return {"configured": True, "connected": False, "error": "Data początkowa musi poprzedzać końcową"}
    if end.timestamp() - start.timestamp() > 24 * 3600:
        return {"configured": True, "connected": False, "error": "Zakres historii nie może przekraczać 24 godzin"}

    auto_step = False
    if not step and output_format == "compact" and end.timestamp() - start.timestamp() > 12 * 3600:
        # pełna doba bez przerzedzenia nie mieści się w jednej odpowiedzi do analizy
        step, auto_step = 120, True
    range_key = f"last-{hours}h" if requested_start is None else f"{start.isoformat()}_{end.isoformat()}"
    filter_key = ",".join(sorted(requested_series or []))
    cache_key = (",".join(channel_ids), f"{range_key}|step={step}|fmt={output_format}|an={int(with_analysis)}|min_opening={minimum_opening}", filter_key)
    now_monotonic = time.monotonic()
    with _history_cache_lock:
        cached = _history_cache.get(cache_key)
        if cached and now_monotonic - cached[0] < HISTORY_CACHE_SECONDS:
            return cached[1]

    series_by_id: dict[str, dict] = {}
    for channel_index, channel_id in enumerate(channel_ids):
        params = {
            "start": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Europe/Warsaw",
            "results": 8000,
        }
        if channel_index < len(read_keys) and read_keys[channel_index]:
            params["api_key"] = read_keys[channel_index]
        request = Request(
            f"https://api.thingspeak.com/channels/{channel_id}/feeds.json?{urlencode(params)}",
            headers={"Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                feeds = json.loads(response.read().decode("utf-8")).get("feeds", [])
        except HTTPError as error:
            raise RuntimeError(
                f"ThingSpeak odrzucił odczyt kanału {channel_id} (HTTP {error.code}); sprawdź klucz Read API i prywatność kanału."
            ) from None
        except Exception:
            raise RuntimeError(f"Nie udało się pobrać historii ThingSpeak z kanału {channel_id}.") from None

        for feed in feeds:
            raw_timestamp = feed.get("created_at")
            if not raw_timestamp:
                continue
            try:
                timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00")).astimezone(local_tz)
            except ValueError:
                continue
            if timestamp < start or timestamp > end:
                continue
            for field_number, (series_id, label, _field_path) in enumerate(fields_by_channel[channel_index], start=1):
                if series_id is None or (requested_series is not None and series_id not in requested_series):
                    continue
                value = _number(feed.get(f"field{field_number}"))
                if value is None or (series_id == "thermoflow-room" and value == 0):
                    continue
                if series_id not in series_by_id:
                    unit = (
                        "%" if "fuel-percent" in series_id or "opening" in series_id
                        else "0/1" if series_id.startswith("thermoflow-pump-")
                        else "kW" if "boiler-power" in series_id
                        else "°C"
                    )
                    series_by_id[series_id] = {
                        "id": series_id, "label": label, "unit": unit,
                        "channel_id": channel_id, "field": field_number,
                        "_points_by_time": {},
                    }
                # Last duplicate at the same series/timestamp wins.
                series_by_id[series_id]["_points_by_time"][int(timestamp.timestamp())] = [int(timestamp.timestamp()), value]

    series = []
    raw_by_id: dict[str, list] = {}
    for item in series_by_id.values():
        raw_points = [point for _, point in sorted(item.pop("_points_by_time").items())]
        raw_by_id[item["id"]] = raw_points
        if step:
            buckets = {}
            for timestamp, value in raw_points:
                bucket = timestamp // step
                stats = buckets.setdefault(bucket, {"last_timestamp": timestamp, "last_value": value, "sum": 0.0, "count": 0})
                stats["last_timestamp"] = timestamp
                stats["last_value"] = value
                stats["sum"] += value
                stats["count"] += 1
            average_series = item["id"] in {
                "thermoflow-co", "thermoflow-dhw", "thermoflow-flue", "thermoflow-outdoor",
                "thermoflow-room", "thermoflow-floor", "thermoflow-valve4d-current",
                "thermoflow-valve4d-return", "thermoflow-boiler-power",
            }
            raw_points = [
                [stats["last_timestamp"], round(stats["sum"] / stats["count"], 1) if average_series else stats["last_value"]]
                for stats in buckets.values()
            ]
        values = [value for _timestamp, value in raw_points]
        if values:
            item["summary"] = {
                "samples": len(values), "min": min(values), "max": max(values),
                "mean": round(sum(values) / len(values), 2), "first": values[0], "last": values[-1],
            }
        if output_format == "full":
            item["format"] = "points"
            item["points"] = raw_points
        elif item["unit"] == "0/1":
            item["format"] = "intervals"
            item["intervals"] = _intervals(raw_points)
            item["on_minutes"] = round(sum(end - begin for begin, end in item["intervals"]) / 60, 1)
        else:
            item["format"] = "changes"
            item["points"] = _compress_changes(raw_points)
        series.append(item)
    order = {series_id: index for index, series_id in enumerate(HISTORY_SERIES_ORDER)}
    series.sort(key=lambda item: (order.get(item["id"], len(order)), item["label"]))

    result = {
        "configured": True,
        "connected": True,
        "source": "ThingSpeak",
        "range_hours": round((end.timestamp() - start.timestamp()) / 3600, 2),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "resolution": "Próbki zapisane w ThingSpeak (zwykle co około 40 sekund).",
        "point_format": (
            "[unix_timestamp_seconds, value]" if output_format == "full" else
            "changes: [unix_timestamp_seconds, value] tylko przy zmianie wartości (wartość obowiązuje do następnego punktu) "
            "+ ostatnia próbka; intervals: [[start, koniec], ...] okresy pracy pomp"
        ),
        "step_seconds": step or None,
        "step_automatic": auto_step,
        "downsampling": "Średnia w przedziale dla temperatur i mocy; ostatnia wartość dla nastaw, otwarcia, pomp i paliwa." if step else None,
        "series": series,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if with_analysis and requested_series is not None:
        result["analysis"] = {"error": "Analiza wymaga pełnego zestawu serii; pomiń parametr series albo ustaw analysis=0"}
    elif with_analysis:
        try:
            # FIRE is stored in its own ThingSpeak channel, outside the two
            # compact history channels. Join it into the same timestamp series
            # before ThermoFlow runs; never make its availability a dependency.
            raw_by_id.update(_read_thermoflow_fire_history(start, end, local_tz))
            result["analysis"] = _history_analysis(raw_by_id, minimum_opening)
            try:
                from thermoflow_diagnostic_intelligence import build_thermoflow_intelligence
                result["analysis"]["thermoflow_intelligence"] = build_thermoflow_intelligence(
                    raw_by_id, result["analysis"]
                )
            except Exception as error:
                # Additive diagnostics must never break the existing history response.
                result["analysis"]["thermoflow_intelligence"] = {
                    "status": "unavailable", "error": f"{type(error).__name__}: {error}"[:180],
                    "safety": "diagnostics_only; no control, setpoint, pump, valve or fuel-estimate changes",
                }
        except Exception as error:   # analiza nie może zablokować samych danych
            result["analysis"] = {"error": str(error)[:180]}
    with _history_cache_lock:
        _history_cache[cache_key] = (now_monotonic, result)
    return result


def _read_snapshot() -> dict:
    token = _token()
    if not token:
        return {"configured": False, "connected": False, "error": "Brak tokenu eModul", "source": "demo"}

    uid = _user_id(token)
    modules = _get(f"users/{uid}/modules", token)
    if not isinstance(modules, list) or not modules:
        raise RuntimeError("Konto eModul nie zwróciło sterowników")

    wanted = os.getenv("TECH_MODULE_ID", "").strip()
    module = next((m for m in modules if str(m.get("udid", "")) == wanted), None) if wanted else None
    if wanted and module is None:
        raise RuntimeError("TECH_MODULE_ID nie pasuje do sterownika na koncie eModul")
    if not wanted:
        module = modules[0]

    udid = module.get("udid")
    if not udid:
        raise RuntimeError("Nie udało się ustalić identyfikatora sterownika")
    details = _get(f"users/{uid}/modules/{udid}", token)
    translations = _translations(token)

    data = {
        "temperatures": {"co": None, "dhw": None, "flue": None, "outdoor": None, "room": None, "buffer_upper": None},
        "boiler_power_kw": None,
        "boiler_power_at": None,
        "pumps": {"floor": None, "valve4d": None, "dhw": None, "circulation": None},
        "valve": {
            "current": None,
            "return": None,
            "set": None,
            "opening": None,
            "set_correction": None,
            "room_reduction": None,
        },
        "fuel": {"percent": None, "hours": None},
        "controller_metrics": {"feeder_temperature_c": None, "instant_fuel_consumption": None},
        "fire": None,
        "fire_signal": None,
        "fire_brightness_estimate": None,
        "fan": {"running": None, "percent": None, "rpm": None},
        "disinfection": {"active": None, "percent": None},
        "mode": None,
        "controller_status": None,
        "status_tiles": [],
        "setpoints": {"co": None, "dhw": None, "room": None},
        "hysteresis": {"boiler": None, "dhw": None, "valve": None},
        "other_tiles": [],
    }
    for tile in details.get("tiles", []):
        params = tile.get("params") or {}
        kind = tile.get("type")
        label = _label(params, translations)
        normalized = label.casefold()

        # Readiness notices may be returned either as plain text (type 40)
        # or as the status badge on a universal widget tile (type 6).
        if kind in (6, 40):
            header = _label(params, translations, "headerId")
            status = _label(params, translations, "statusId")
            option_ids = params.get("options")
            option_ids = option_ids if isinstance(option_ids, list) else []
            options = [FALLBACK_LABELS.get(str(option_id)) or translations.get(str(option_id), "") for option_id in option_ids]
            options = [option for option in options if option]
            if header or status:
                data["status_tiles"].append({"header": header, "status": status, "options": options})
            if kind == 6:
                unit_divisors = {4: 10, 5: 100, 7: 10, 23: 10, 26: 10, 33: 10}
                for slot in ("widget1", "widget2"):
                    widget = params.get(slot)
                    if not isinstance(widget, dict) or widget.get("txtId", 0) == 0:
                        continue
                    if widget.get("unit") in (6, 8):
                        continue
                    if widget.get("unit") == -1 and widget.get("type") == 0:
                        continue
                    widget_label = _label(widget, translations)
                    widget_value = _number(
                        widget.get("value"),
                        unit_divisors.get(widget.get("unit"), 1),
                    )
                    _capture_controller_metric(data, widget_label, widget.get("value"), unit=widget.get("unit"))
                    if widget_label and widget_value is not None and data["controller_metrics"]["feeder_temperature_c"] is None:
                        data["other_tiles"].append({
                            "label": widget_label,
                            "value": widget_value,
                            "unit": "°C",
                        })

        if kind == 1:
            value = _number(params.get("value"), 10)
            _capture_controller_metric(data, label, params.get("value"))
            if "temperatura co" in normalized:
                data["temperatures"]["co"] = value
            elif "c.w.u" in normalized or "cwu" in normalized:
                data["temperatures"]["dhw"] = value
            elif "spalin" in normalized:
                data["temperatures"]["flue"] = value
            elif "zewnętrz" in normalized or "zewnetrz" in normalized:
                data["temperatures"]["outdoor"] = value
            elif "temperatura pokoju" in normalized or "temperatura pomieszczenia" in normalized:
                data["temperatures"]["room"] = value
            elif "powrot" in normalized:
                data["temperatures"]["return"] = value
            elif label and data["controller_metrics"]["feeder_temperature_c"] is None:
                data["other_tiles"].append({"label": label, "value": value, "unit": "°C"})
        elif kind == 11:
            running = bool(params.get("workingStatus"))
            if "pompa co" in normalized:
                # In this installation the controller's CO output drives the
                # underfloor loop, as confirmed by the user's wiring diagram.
                data["pumps"]["floor"] = running
            elif "pompa cwu" in normalized or "pompa c.w.u" in normalized:
                data["pumps"]["dhw"] = running
            elif "podajnik" in normalized:
                data.setdefault("outputs", {})["feeder"] = running
            elif "grzał" in normalized or "grzal" in normalized:
                data.setdefault("outputs", {})["heater"] = running
            elif label:
                data.setdefault("outputs", {})[label] = running
        elif kind == 21:
            # In this installation TECH's "additional pump" is the DHW
            # circulation pump; expose one unambiguous live status.
            data["pumps"]["circulation"] = bool(params.get("workingStatus"))
        elif kind == 22:
            data["fan"] = {
                "running": bool(params.get("workingStatus")),
                "percent": params.get("gear"),
                "rpm": params.get("rpm", params.get("speed")),
            }
        elif kind == 23:
            # valvePump is the live state of the pump assigned to the built-in
            # 4D valve; the CO relay above is wired to the underfloor pump.
            valve_pump = params.get("valvePump")
            if valve_pump is not None:
                data["pumps"]["valve4d"] = bool(valve_pump)
            data["valve"] = {
                "current": _number(params.get("currentTemp"), 10),
                "return": _number(params.get("returnTemp"), 10),
                "set": _number(params.get("setTemp")),
                "opening": params.get("openingPercentage"),
                "set_correction": _number(params.get("setTempCorrection")),
                "working": bool(params.get("workingStatus")),
            }
        elif kind == 31:
            hours = params.get("hours")
            # eModul sends this type-31 total-hours field as a signed byte.
            # Convert wrapped values (for example -106 => 150 h) back to
            # their unsigned total before the dashboard formats days/hours.
            if isinstance(hours, (int, float)) and not isinstance(hours, bool) and -128 <= hours < 0:
                hours += 256
            data["fuel"] = {
                "percent": params.get("percentage"),
                "hours": hours if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours >= 0 else None,
            }
        elif kind == 2:
            data["fire"] = params.get("workingStatus")
            data["fire_signal"] = _number(params.get("value"))
            # Match eModul's own fire-tile rendering: values above 100 are
            # already brightness readings; lower raw values are scaled by 82.
            signal = data["fire_signal"]
            data["fire_brightness"] = (signal if signal > 100 else signal * 82) if signal is not None else None
        elif kind == 32:
            data["disinfection"] = {
                "active": bool(params.get("percentage")),
                "percent": params.get("percentage"),
            }
        elif kind == 40:
            header = _label(params, translations, "headerId")
            status = _label(params, translations, "statusId")
            if "tryb pracy" in header.casefold():
                data["mode"] = status
            elif "stan sterownika" in header.casefold():
                data["controller_status"] = status
        elif kind == 50:
            data["software"] = {
                "company": module.get("company"),
                "controller": params.get("controllerName"),
                "module_version": params.get("version"),
                "version": module.get("version"),
            }

    buffer_upper, buffer_upper_at, boiler_power_kw, boiler_power_at, discovered_metrics = _live_statistics(udid, token)
    data["controller_metrics"].update({
        key: value for key, value in discovered_metrics.items()
        if key in data["controller_metrics"] and data["controller_metrics"][key] is None
    })
    data["temperatures"]["buffer_upper"] = buffer_upper
    data["temperatures"]["buffer_upper_at"] = buffer_upper_at
    # eModul's series 3159 ("Moc chwilowa") is already expressed in kW.
    data["boiler_power_kw"] = boiler_power_kw
    data["boiler_power_at"] = boiler_power_at

    # User/installer menus are read-only here. They can expose boiler/DHW
    # setpoints that are not included in the dashboard temperature tiles.
    for menu_type in ("MU", "MI"):
        try:
            menu_body = _get(f"users/{uid}/modules/{udid}/menu/{menu_type}/", token)
        except Exception:
            continue
        for item in menu_body.get("data", {}).get("elements", []):
            params = item.get("params") or {}
            label = FALLBACK_LABELS.get(str(item.get("txtId", ""))) or translations.get(str(item.get("txtId", "")), "")
            normalized = re.sub(r"\s+", " ", label.casefold().replace(".", ""))
            raw_value = params.get("value")
            value = _number(raw_value, 10 if params.get("format") == 2 else 1)
            # ST-9730 menu values: the built-in valve hysteresis is item 2250
            # and uses format 2 (tenths of a degree).
            if item.get("id") == 2062 or "histereza kot" in normalized:
                data["hysteresis"]["boiler"] = value
            elif item.get("id") == 2063 or "histereza cwu" in normalized:
                data["hysteresis"]["dhw"] = value
            elif item.get("id") == 2250:
                data["hysteresis"]["valve"] = value
            elif item.get("id") == 2208 or "obnizenie pokojowki" in normalized:
                # Built-in valve 1 → Room regulator → Room thermostat setback.
                data["valve"]["room_reduction"] = value
            if item.get("id") == 2060:
                data["setpoints"]["co"] = value
            elif item.get("id") == 2061:
                data["setpoints"]["dhw"] = value
            elif item.get("id") == 2089:
                data["setpoints"]["room"] = value
            if "zadana" in normalized or "zadanej" in normalized or "nastawa" in normalized:
                if any(term in normalized for term in ("cwu", "cieplej wody", "ciepłej wody")):
                    data["setpoints"]["dhw"] = value
                elif "co" in normalized or "kotla" in normalized or "kotła" in normalized:
                    data["setpoints"]["co"] = value

    valve_target = data["valve"].get("set")
    valve_correction = data["valve"].get("set_correction")
    if valve_target is not None and valve_correction is not None:
        data["valve"]["set_after_correction"] = round(valve_target + valve_correction, 1)

    return {
        "configured": True,
        "connected": True,
        "source": "eModul",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "controller": {
            "module_id": str(udid),
            "name": module.get("name"),
            "company": module.get("company"),
            "type": module.get("type"),
            "version": module.get("version"),
            "controller_status": module.get("controllerStatus"),
        },
        "data": data,
    }


def _read_modules() -> dict:
    """Return controller names and IDs so an installer can select a module."""
    token = _token()
    if not token:
        return {"configured": False, "connected": False, "error": "Brak tokenu eModul", "modules": []}

    uid = _user_id(token)
    modules = _get(f"users/{uid}/modules", token)
    if not isinstance(modules, list):
        raise RuntimeError("Konto eModul zwróciło nieprawidłową listę sterowników")

    selected_id = os.getenv("TECH_MODULE_ID", "").strip()
    default_id = str(modules[0].get("udid", "")) if modules and isinstance(modules[0], dict) else ""
    selected_id = selected_id or default_id
    return {
        "configured": True,
        "connected": True,
        "source": "eModul",
        "selected_id": selected_id or None,
        "modules": [
            {
                "name": module.get("name") or "Sterownik bez nazwy",
                "udid": module.get("udid"),
                "type": module.get("type"),
                "version": module.get("version"),
                "selected": str(module.get("udid", "")) == selected_id,
            }
            for module in modules
            if isinstance(module, dict)
        ],
    }


def _read_diagnostics() -> dict:
    """Expose the controller's available read-only tiles, zones, and menus."""
    token = _token()
    if not token:
        return {"configured": False, "connected": False, "error": "Brak tokenu eModul"}

    uid = _user_id(token)
    modules = _get(f"users/{uid}/modules", token)
    if not isinstance(modules, list) or not modules:
        raise RuntimeError("Konto eModul nie zwróciło sterowników")

    wanted = os.getenv("TECH_MODULE_ID", "").strip()
    module = next((item for item in modules if str(item.get("udid", "")) == wanted), None) if wanted else modules[0]
    if module is None:
        raise RuntimeError("TECH_MODULE_ID nie pasuje do sterownika na koncie eModul")

    udid = module.get("udid")
    details = _get(f"users/{uid}/modules/{udid}", token)
    translations = _translations(token)
    tile_types = {
        1: "temperatura",
        2: "czujnik ognia",
        6: "status z widgetami",
        11: "przekaźnik / pompa",
        21: "pompa dodatkowa",
        22: "wentylator",
        23: "zawór mieszający",
        24: "zewnętrzny zawór mieszający",
        31: "paliwo",
        32: "dezynfekcja",
        40: "status tekstowy",
        50: "wersja oprogramowania",
        252: "OpenTherm",
    }
    unit_divisors = {4: 10, 5: 100, 7: 10, 22: 10, 23: 10, 26: 10, 30: 100, 33: 10}

    tiles = []
    for tile in details.get("tiles", []):
        params = tile.get("params") or {}
        entry = {
            "id": tile.get("id"),
            "type": tile.get("type"),
            "type_name": tile_types.get(tile.get("type"), "inny"),
            "visible": tile.get("visibility"),
            "label": _label(params, translations),
            "header": _label(params, translations, "headerId"),
            "status": _label(params, translations, "statusId"),
            "params": params,
        }
        if tile.get("type") in (6, 40):
            option_ids = params.get("options")
            option_ids = option_ids if isinstance(option_ids, list) else []
            entry["options"] = [
                {
                    "id": option_id,
                    "label": FALLBACK_LABELS.get(str(option_id)) or translations.get(str(option_id), ""),
                }
                for option_id in option_ids
            ]
        if tile.get("type") == 6:
            entry["widgets"] = []
            for slot in ("widget1", "widget2"):
                widget = params.get(slot)
                if not isinstance(widget, dict):
                    continue
                raw_value = widget.get("value")
                divisor = unit_divisors.get(widget.get("unit"), 1)
                entry["widgets"].append({
                    "slot": slot,
                    "label": _label(widget, translations),
                    "type": widget.get("type"),
                    "unit_code": widget.get("unit"),
                    "unit": {22: "L/min", 30: "L/h"}.get(widget.get("unit")),
                    "raw_value": raw_value,
                    "value": _number(raw_value, divisor),
                })
        tiles.append(entry)

    menus = []
    menu_access = {}
    # eModul defines user, installer, service and manufacturer menus. Some
    # are unavailable to ordinary accounts or require a controller code;
    # retain that fact in diagnostics instead of silently hiding the menu.
    for menu_type in ("MU", "MI", "MS", "MP"):
        try:
            menu_body = _get(f"users/{uid}/modules/{udid}/menu/{menu_type}/", token)
            elements = (menu_body.get("data") or {}).get("elements", [])
            menu_access[menu_type] = {"available": True, "count": len(elements)}
        except Exception as error:
            menu_access[menu_type] = {"available": False, "error": str(error)[:180]}
            continue
        for item in elements:
            menus.append({
                "menu": menu_type,
                "id": item.get("id"),
                "type": item.get("type"),
                "label": translations.get(str(item.get("txtId", "")), ""),
                "params": item.get("params") or {},
            })

    zones_body = details.get("zones") or {}
    zones = zones_body.get("elements", []) if isinstance(zones_body, dict) else []
    # Preserve the complete eModul zones document. The dashboard's live
    # mapper only needs `elements`, but system-controller metadata (signal,
    # relays, contacts and schedules) lives beside that list.
    zones_data = zones_body if isinstance(zones_body, dict) else {}
    # Statistics endpoints include time-series that are not part of the live
    # tile snapshot (for example the upper buffer sensor). Keep this probe in
    # diagnostics only; the dashboard's regular polling should stay lightweight.
    statistics = {
        "available": None,
        "latest": {},
        "series": {},
        "consumptions_day": None,
        "consumptions_error": None,
        "error": None,
    }
    try:
        available = _get(f"modules/{udid}/statistics/available", token)
        linear = _get(f"modules/{udid}/statistics/linear/range/day", token)
        linear_data = linear.get("data") or {}
        history = linear_data.get("history") or {}
        descriptions = linear_data.get("descriptions") or {}
        if isinstance(descriptions, list):
            descriptions = {
                str(series_id): descriptions[index]
                for index, series_id in enumerate(history)
                if index < len(descriptions)
            }
        elif not isinstance(descriptions, dict):
            descriptions = {}
        else:
            descriptions = {str(key): value for key, value in descriptions.items()}
        statistics["available"] = available
        # Keep only a compact summary here. Full x/y samples are available
        # from /api/history?hours=6 in a readable timestamp/value format.
        for series_id, points in history.items():
            if not isinstance(points, list) or not points:
                continue
            valid_points = [point for point in points if isinstance(point, dict)]
            if not valid_points:
                continue
            latest = max(valid_points, key=lambda point: str(point.get("x", "")))
            key = str(series_id)
            latest_summary = {
                "timestamp": _history_timestamp(latest.get("x")).isoformat()
                if _history_timestamp(latest.get("x")) else None,
                "value": _number(latest.get("y")),
            }
            statistics["latest"][key] = latest_summary
            statistics["series"][key] = {
                "description": descriptions.get(key),
                "latest": latest_summary,
                "points_count": len(valid_points),
            }
    except Exception as error:
        statistics["error"] = str(error)[:180]
    # This endpoint is a daily/monthly aggregate. Keep it in diagnostics only;
    # it must not be presented as the controller's instantaneous fuel rate.
    try:
        statistics["consumptions_day"] = _get(f"modules/{udid}/statistics/consumptions/range/day", token)
    except Exception as error:
        statistics["consumptions_error"] = str(error)[:180]
    # Centralne parametry analizy. Nastawy kotła i zaworu pochodzą z menu
    # eModul; stałe opisują wyłącznie reguły interpretacji zapisanej telemetrii.
    menu_by_id = {str(item.get("id")): item for item in menus}
    def configured_menu_value(item_id):
        item = menu_by_id.get(str(item_id))
        params = (item or {}).get("params") or {}
        raw = _number(params.get("value"), 10 if str(params.get("format")) == "2" else 1)
        return raw

    co_target = configured_menu_value(2060)
    boiler_hysteresis = configured_menu_value(2062)
    valve_minimum_opening = configured_menu_value(2203)
    analysis_config = {
        "co_recovery_target": co_target,
        "co_low_threshold": (
            round(co_target - boiler_hysteresis, 1)
            if co_target is not None and boiler_hysteresis is not None else None
        ),
        "return_protection_min": 55.0,
        "return_protection_tolerance": 1.0,
        "valve_minimum_opening": valve_minimum_opening,
        "max_sample_gap_seconds": 300,
        "pump_correlation_window_seconds": 600,
        "boiler_power_active_min_kw": 0.0,
        "valve_change_min_delta": 3.0,
    }

    return {
        "configured": True,
        "connected": True,
        "source": "eModul",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "controller": {
            "name": module.get("name"),
            "type": module.get("type"),
            "version": module.get("version"),
            "metadata": {
                key: value
                for key, value in module.items()
                if key not in {"name", "type", "version", "udid"}
            },
        },
        "counts": {"tiles": len(tiles), "zones": len(zones), "menu_items": len(menus)},
        "analysis_config": analysis_config,
        "tiles": tiles,
        "zones": zones,
        "zones_data": zones_data,
        "menus": menus,
        "menu_access": menu_access,
        "statistics": statistics,
    }


def snapshot() -> dict:
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and now - _cache["updated"] < POLL_CACHE_SECONDS:
            return _cache["payload"]
        try:
            payload = _read_snapshot()
            if payload.get("connected"):
                _thingspeak_write(payload)
                _thingspeak_write_fire(payload)
        except HTTPError as error:
            payload = {
                "configured": True,
                "connected": False,
                "source": "eModul",
                "error": "eModul odrzucił token lub żądanie (HTTP " + str(error.code) + ")",
            }
        except (URLError, TimeoutError, OSError):
            payload = {"configured": bool(_token()), "connected": False, "source": "eModul", "error": "Brak połączenia z eModul"}
        except Exception as error:
            payload = {"configured": bool(_token()), "connected": False, "source": "eModul", "error": str(error)[:180]}
        _cache.update(updated=now, payload=payload)
        return payload


_fuel_engine = None
_fuel_import_error = None


def _load_fuel_engine():
    """Load the optional fuel subsystem lazily; core eModul routes must stay independent."""
    global _fuel_engine, _fuel_import_error
    if _fuel_engine is not None:
        return _fuel_engine
    if _fuel_import_error is not None:
        return None
    try:
        import importlib
        interface = importlib.import_module("fuel_engine")
        _fuel_engine = interface.load()
    except Exception as error:
        _fuel_import_error = f"{type(error).__name__}: {error}"[:180]
        return None
    return _fuel_engine


def _warmup_fuel():
    """Warm the optional fuel subsystem after the HTTP server has started."""
    engine = _load_fuel_engine()
    if engine is not None:
        try:
            engine.warmup()
        except Exception:
            # Fuel warmup must never prevent the core API from serving requests.
            return


def _fuel_summary_for_intelligence(headers):
    """Reuse the existing cached fuel summary; ML must never trigger fuel writes."""
    engine = _load_fuel_engine()
    if engine is None:
        return None
    try:
        result = engine.handle("GET", "/api/fuel/summary", {}, headers, b"")
        if result and result[0] == 200 and isinstance(result[1], dict):
            return result[1]
    except Exception:
        return None
    return None


def _api_error_status(error: Exception) -> int:
    """Map API failures to stable HTTP status codes without changing payload shape."""
    if isinstance(error, ValueError):
        return 400
    if isinstance(error, HTTPError):
        return error.code if 400 <= error.code < 500 else 502
    if isinstance(error, TimeoutError):
        return 504
    if isinstance(error, (URLError, OSError)):
        return 503
    return 500


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_fuel(self, method: str) -> bool:
        """Zużycie peletu: osobny moduł fuel.py, dane w osobnym kanale ThingSpeak."""
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/fuel/"):
            return False
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        engine = _load_fuel_engine()
        if engine is None:
            self._send_json(503, {"error": "Silnik paliwa jest chwilowo niedostępny"})
            return True
        try:
            result = engine.handle(method, parsed.path, parse_qs(parsed.query), self.headers, body)
        except Exception as error:
            result = (500, {"error": str(error)[:180]})
        if result is None:
            return False
        self._send_json(*result)
        return True

    def _handle_fibaro(self, method: str) -> bool:
        parsed = urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/fibaro/"):
            return False
        if path not in ("/api/fibaro/dashboard", "/api/fibaro/comfort", "/api/fibaro/health", "/api/fibaro/snapshot", "/api/fibaro/diagnostics", "/api/fibaro/telemetry", "/api/fibaro/history", "/api/fibaro/store/status", "/api/fibaro/store/history", "/api/fibaro/store/shadow", "/api/fibaro/store/analytics"):
            self._send_json(404, {"error": "Nieznany endpoint Fibaro HC3"})
            return True
        if method != "GET":
            self._send_json(405, {"error": "Fibaro HC3 API udostępnia wyłącznie GET"})
            return True

        # Bounded read-only projection from the shared SQLite archive.
        # This endpoint never contacts HC3 or exposes credentials/raw head payloads.
        if path == "/api/fibaro/dashboard":
            query = parse_qs(parsed.query)
            try:
                days = int(query.get("days", ["14"])[0])
                result = read_fibaro_dashboard(
                    os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3"),
                    days=days,
                )
                self._send_json(200, result)
            except FileNotFoundError:
                self._send_json(503, {"source": "shared-sqlite",
                                      "error": "Baza ThermoFlow SQLite nie jest zainicjalizowana"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)[:180]})
            except RuntimeError:
                self._send_json(503, {"source": "shared-sqlite",
                                      "error": "Schemat bazy ThermoFlow SQLite jest niegotowy"})
            except Exception:
                self._send_json(500, {"source": "shared-sqlite",
                                      "error": "Nie udało się odczytać danych Fibaro z lokalnej bazy"})
            return True

        # Rule-based "cold zones and why" projection; SQLite only, never contacts HC3.
        if path == "/api/fibaro/comfort":
            query = parse_qs(parsed.query)
            try:
                hours = int(query.get("hours", ["24"])[0])
                cached = _COMFORT_CACHE.get(hours)
                if cached and time.time() - cached[0] < _COMFORT_CACHE_SECONDS:
                    result = cached[1]
                else:
                    result = read_fibaro_comfort(
                        os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3"),
                        hours=hours,
                    )
                    _COMFORT_CACHE[hours] = (time.time(), result)
                self._send_json(200, result)
            except FileNotFoundError:
                self._send_json(503, {"source": "shared-sqlite",
                                      "error": "Baza ThermoFlow SQLite nie jest zainicjalizowana"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)[:180]})
            except RuntimeError:
                self._send_json(503, {"source": "shared-sqlite",
                                      "error": "Schemat bazy ThermoFlow SQLite jest niegotowy"})
            except Exception:
                self._send_json(500, {"source": "shared-sqlite",
                                      "error": "Nie udało się policzyć stanu stref z lokalnej bazy"})
            return True

        expected_token = os.getenv("FIBARO_HC3_API_TOKEN", "").strip()
        if not expected_token:
            self._send_json(503, {"configured": False, "connected": False,
                                  "error": "Brak tokenu dostępu do API Fibaro HC3"})
            return True
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {expected_token}"):
            self._send_json(401, {"error": "Brak autoryzacji API Fibaro HC3"})
            return True

        if path.startswith("/api/fibaro/store/"):
            query = parse_qs(parsed.query)
            database_path = os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3")
            try:
                if path == "/api/fibaro/store/status":
                    result = read_fibaro_store_status(database_path)
                else:
                    if "start" not in query or "end" not in query:
                        raise ValueError("Podaj wymagane parametry start i end (Unix seconds)")
                    start, end = int(query["start"][0]), int(query["end"][0])
                    if path == "/api/fibaro/store/history":
                        result = read_fibaro_store_history(database_path, start=start, end=end)
                    elif path == "/api/fibaro/store/shadow":
                        result = read_fibaro_store_shadow(database_path, start=start, end=end)
                    else:
                        tolerance = int(query.get("tolerance_seconds", ["90"])[0])
                        timezone_name = query.get("timezone", ["Europe/Warsaw"])[0]
                        result = read_heating_analytics(
                            database_path, start=start, end=end,
                            tolerance_seconds=tolerance, timezone_name=timezone_name,
                        )
                self._send_json(200, result)
            except FileNotFoundError:
                self._send_json(503, {"configured": False, "source": "shared-sqlite",
                                      "error": "Baza ThermoFlow SQLite nie jest zainicjalizowana"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)[:180]})
            except RuntimeError:
                self._send_json(503, {"source": "shared-sqlite",
                                      "error": "Schemat bazy ThermoFlow SQLite jest niegotowy"})
            except Exception:
                self._send_json(500, {"source": "shared-sqlite",
                                      "error": "Nie udało się odczytać danych z lokalnej bazy"})
            return True

        history_start = history_end = None
        if path == "/api/fibaro/history":
            query = parse_qs(parsed.query)
            try:
                if "start" not in query or "end" not in query:
                    raise ValueError("Podaj wymagane parametry start i end (Unix seconds)")
                history_start = int(query["start"][0])
                history_end = int(query["end"][0])
                if history_start < 0 or history_end <= history_start:
                    raise ValueError("Zakres historii wymaga poprawnych znaczników Unix start < end")
                if history_end - history_start > MAX_HISTORY_SECONDS:
                    raise ValueError("Historia Fibaro HC3 jest ograniczona do maksymalnie 7 dni")
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Nieprawidłowy zakres historii; podaj start i end w Unix seconds, maksymalnie 7 dni"})
                return True

        try:
            config = HC3Config.from_env()
        except HC3Error as error:
            self._send_json(error.status, {
                "configured": False,
                "connected": False,
                "source": "fibaro-hc3",
                "error": error.message,
            })
            return True

        try:
            provider = FibaroHC3Provider(config)
            if path == "/api/fibaro/health":
                zones = provider.get_climate_zones()
                self._send_json(200, {
                    "configured": True,
                    "connected": True,
                    "source": "fibaro-hc3",
                    "zone_count": len(zones),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
            elif path == "/api/fibaro/snapshot":
                self._send_json(200, build_hc3_snapshot(provider))
            elif path == "/api/fibaro/diagnostics":
                snapshot = build_hc3_snapshot(provider)
                self._send_json(200, build_fibaro_zone_diagnostics(snapshot))
            elif path == "/api/fibaro/telemetry":
                snapshot = build_hc3_snapshot(provider)
                diagnostics = build_fibaro_zone_diagnostics(snapshot)
                self._send_json(200, build_fibaro_telemetry(snapshot, diagnostics))
            else:
                snapshot = build_hc3_snapshot(provider)
                events = provider.get_events_range(history_start, history_end)
                self._send_json(200, analyze_fibaro_event_history(
                    snapshot, events, history_start, history_end
                ))
        except HC3Error as error:
            self._send_json(error.status, {
                "configured": True,
                "connected": False,
                "source": "fibaro-hc3",
                "error": error.message,
            })
        except ValueError as error:
            self._send_json(400, {"error": str(error)[:180]})
        except Exception:
            self._send_json(502, {
                "configured": True,
                "connected": False,
                "source": "fibaro-hc3",
                "error": "Nie udało się pobrać danych z Fibaro HC3",
            })
        return True

    def do_POST(self):
        if self._handle_fibaro("POST"):
            return
        if not self._handle_fuel("POST"):
            self.send_error(404)

    def do_PUT(self):
        if self._handle_fibaro("PUT"):
            return
        self.send_error(404)

    def do_PATCH(self):
        if self._handle_fibaro("PATCH"):
            return
        self.send_error(404)

    def do_DELETE(self):
        if self._handle_fibaro("DELETE"):
            return
        if not self._handle_fuel("DELETE"):
            self.send_error(404)

    def do_GET(self):
        if self._handle_fibaro("GET"):
            return
        if self._handle_fuel("GET"):
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/thermoflow/data-readiness":
            query = parse_qs(parsed.query)
            try:
                days = int(query.get("days", ["30"])[0])
                payload = read_sqlite_data_audit(
                    os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3"),
                    now=int(time.time()),
                    days=days,
                )
                self._send_json(200, payload)
            except FileNotFoundError:
                self._send_json(503, {"status": "unavailable",
                                      "error": "Baza ThermoFlow SQLite nie jest zainicjalizowana"})
            except ValueError as error:
                self._send_json(400, {"error": str(error)[:180]})
            except Exception:
                self._send_json(500, {"status": "blocked",
                                      "error": "Nie udało się wykonać audytu danych ML/AI"})
            return

        if path == MODEL_MONITORING_PATH:
            try:
                payload = monitor_sqlite_database(
                    os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3"),
                    now=int(time.time()),
                )
            except Exception:
                payload = {
                    "schema": "thermoflow-model-monitoring/1",
                    "as_of": int(time.time()),
                    "mode": "read_only_shadow_monitoring",
                    "status": "collecting",
                    "promotion": "disabled",
                    "error": "monitoring_unavailable",
                    "modules": {},
                }
            self._send_json(200, payload)
            return
        if path == SHADOW_PATH:
            self._send_json(200, _shadow_runtime.read(time.time()))
            return
        if path == INTELLIGENCE_PATH:
            now = time.time()
            fuel_summary = (
                _fuel_summary_for_intelligence(self.headers)
                if _live_ml_runtime.fuel_path else None
            )
            shadow = _shadow_runtime.read(now)
            response = _intelligence_runtime.read(now, shadow)
            response["weather_forecast"] = project_weather_forecast(fuel_summary, now=now)
            response["live_predictions"] = _live_ml_runtime.read(
                now, fuel_summary if _live_ml_runtime.fuel_path else None
            )
            response["digital_twin"] = shadow.get("digital_twin")
            self._send_json(200, response)
            return
        if path == ACCEPTANCE_PATH:
            now = time.time()
            shadow = _shadow_runtime.read(now)
            # Acceptance is a local projection only: do not fetch fuel summary,
            # ThingSpeak or Open-Meteo from this endpoint.
            predictions = _live_ml_runtime.read(now)
            evidence = build_acceptance_evidence(now=now)
            self._send_json(200, build_acceptance_report(
                shadow=shadow, live_predictions=predictions,
                data_quality=evidence["data_quality"],
                monitoring=evidence["monitoring"],
                digital_twin=shadow.get("digital_twin"),
            ))
            return
        if path not in ("/api/status", "/api/health", "/api/modules", "/api/diagnostics", "/api/statistics", "/api/history"):
            self.send_error(404)
            return
        response_status = 200
        if path == "/api/statistics":
            query = parse_qs(parsed.query)
            try:
                period = query.get("period", ["day"])[0]
                year = int(query.get("year", [str(datetime.now().year)])[0])
                month = int(query.get("month", [str(datetime.now().month)])[0])
                payload = _read_statistics(period, year, month)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                response_status = _api_error_status(error)
                if isinstance(error, HTTPError):
                    message = f"ThingSpeak odrzucił żądanie (HTTP {error.code})"
                elif isinstance(error, ValueError):
                    message = str(error)[:180]
                elif isinstance(error, TimeoutError):
                    message = "Przekroczono czas oczekiwania na ThingSpeak"
                else:
                    message = "Brak połączenia z ThingSpeak"
                payload = {"configured": bool(_token()), "connected": False, "error": message}
            except Exception as error:
                response_status = _api_error_status(error)
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180]}
        elif path == "/api/history":
            query = parse_qs(parsed.query)
            try:
                hours = int(query.get("hours", ["6"])[0])
                if not 1 <= hours <= 24:
                    raise ValueError("Parametr hours musi być liczbą od 1 do 24")
                step = int(query.get("step", ["0"])[0])
                if step and not 60 <= step <= 3600:
                    raise ValueError("Parametr step musi wynosić od 60 do 3600 sekund")
                requested_start = requested_end = None
                if "start" in query or "end" in query:
                    if "start" not in query or "end" not in query:
                        raise ValueError("Podaj jednocześnie parametry start i end")
                    requested_start = _history_query_datetime(query["start"][0])
                    requested_end = _history_query_datetime(query["end"][0])
                known_series = {
                    series_id
                    for channel in _thingspeak_field_schema()
                    for series_id, _label, _path in channel
                    if series_id
                }
                requested_series = None
                if "series" in query:
                    names = {name.strip() for value in query["series"] for name in value.split(",") if name.strip()}
                    expanded = set()
                    for name in names:
                        expanded.update(HISTORY_SERIES_ALIASES.get(name, {name}))
                    unknown = expanded - known_series
                    if unknown:
                        raise ValueError("Nieznana seria: " + ", ".join(sorted(unknown)))
                    requested_series = expanded
                output_format = query.get("format", ["compact"])[0]
                if output_format not in ("compact", "full"):
                    raise ValueError("Parametr format przyjmuje wartości compact albo full")
                with_analysis = query.get("analysis", ["1"])[0] not in ("0", "false", "no")
                minimum_opening = None
                if "minimum_opening" in query:
                    try:
                        minimum_opening = float(query["minimum_opening"][0])
                    except (TypeError, ValueError):
                        raise ValueError("Parametr minimum_opening musi być liczbą od 0 do 100")
                    if not 0 <= minimum_opening <= 100:
                        raise ValueError("Parametr minimum_opening musi być liczbą od 0 do 100")
                payload = _read_history(hours, requested_series, requested_start, requested_end, step,
                                        output_format, with_analysis, minimum_opening)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                response_status = _api_error_status(error)
                if isinstance(error, HTTPError):
                    message = f"ThingSpeak odrzucił żądanie (HTTP {error.code})"
                elif isinstance(error, ValueError):
                    message = str(error)[:180]
                elif isinstance(error, TimeoutError):
                    message = "Przekroczono czas oczekiwania na ThingSpeak"
                else:
                    message = "Brak połączenia z ThingSpeak"
                payload = {"configured": bool(_token()), "connected": False, "error": message}
            except Exception as error:
                response_status = _api_error_status(error)
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180]}
        elif path in ("/api/modules", "/api/diagnostics"):
            try:
                payload = _read_modules() if path == "/api/modules" else _read_diagnostics()
            except HTTPError as error:
                response_status = _api_error_status(error)
                payload = {"configured": True, "connected": False, "error": f"eModul odrzucił żądanie (HTTP {error.code})"}
            except (URLError, TimeoutError, OSError) as error:
                response_status = _api_error_status(error)
                message = "Przekroczono czas oczekiwania na eModul" if isinstance(error, TimeoutError) else "Brak połączenia z eModul"
                payload = {"configured": bool(_token()), "connected": False, "error": message}
            except Exception as error:
                response_status = _api_error_status(error)
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180]}
        else:
            payload = snapshot()
            if path == "/api/health":
                payload = {key: payload.get(key) for key in ("configured", "connected", "source", "error", "fetched_at")}
        self._send_json(response_status, payload)

    def log_message(self, _format, *_args):
        # Do not emit credentials or account-specific API response bodies.
        return


def _poll_forever() -> None:
    """Keep ThingSpeak history recording even when no dashboard tab is open."""
    while True:
        payload = snapshot()
        # Persist only allow-listed AI/ML measurements independently of
        # dashboard traffic. The helper is fail-soft and never runs migrations.
        try:
            persist_emodul_snapshot(
                payload,
                database_path=os.getenv("THERMOFLOW_DB_PATH", "/var/lib/thermoflow/thermoflow.sqlite3"),
                module_id=(payload.get("controller") or {}).get("module_id") or os.getenv("TECH_MODULE_ID") or "default",
            )
        except Exception:
            # Storage is an archival side path; never interrupt the live poller.
            pass
        try:
            _shadow_runtime.observe(payload)
        except Exception:
            # Optional screening must not stop the existing archival poller.
            _shadow_runtime.observe({})
        try:
            _live_ml_runtime.observe(payload)
        except Exception:
            _live_ml_runtime.observe({})
        time.sleep(30)


def _refresh_intelligence_forever() -> None:
    while True:
        now = time.time()
        _intelligence_runtime.refresh(now=now)
        _live_ml_runtime.refresh(now=now)
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=_poll_forever, name="eModul-poller", daemon=True).start()
    try:
        _analytics_scheduler_thread = start_background_scheduler()
    except Exception as error:
        print(json.dumps({
            "component": "thermoflow-analytics",
            "status": "not_started",
            "error_type": type(error).__name__,
        }), flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    threading.Thread(target=_warmup_fuel, name="fuel-warmup", daemon=True).start()
    if _intelligence_bundle or _live_ml_runtime.configured:
        threading.Thread(target=_refresh_intelligence_forever, name="intelligence-bundle", daemon=True).start()
    server.serve_forever()
