"""Read-only TECH/eModul data bridge for the ThermoFlow dashboard."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://emodul.eu/api/v1/"
POLL_CACHE_SECONDS = 20
STATISTICS_CACHE_SECONDS = 30
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
_statistics_cache: dict[str, tuple[float, float | None, str | None, float | None, str | None, dict]] = {}
_statistics_lock = threading.Lock()
_chart_cache: dict[tuple[str, str, int, int], tuple[float, dict]] = {}
_chart_cache_lock = threading.Lock()
_i18n_cache: dict[str, str] | None = None
VALVE_OPENING_SERIES_ID = "thermoflow-valve4d-opening"
VALVE_OPENING_SERIES_LABEL = "Otwarcie zaworu 4D"
VALVE_SETPOINT_SERIES_ID = "thermoflow-valve4d-setpoint"
VALVE_SETPOINT_SERIES_LABEL = "Temperatura zadana zaworu 4D"
THINGSPEAK_WRITE_INTERVAL_SECONDS = max(35, int(os.getenv("THINGSPEAK_WRITE_INTERVAL_SECONDS", "40")))
THINGSPEAK_LAST_WRITES = [0.0, 0.0, 0.0]
THINGSPEAK_WRITE_LOCK = threading.Lock()
THINGSPEAK_CHANNEL_FIELDS = (
    (
        ("thermoflow-co", "Temperatura CO", "temperatures.co"),
        ("thermoflow-dhw", "Temperatura CWU", "temperatures.dhw"),
        ("thermoflow-flue", "Temperatura spalin", "temperatures.flue"),
        ("thermoflow-outdoor", "Temperatura zewnętrzna", "temperatures.outdoor"),
        ("thermoflow-room", "Temperatura pokoju", "temperatures.room"),
        ("thermoflow-floor", "Temperatura podłogówki (C1)", "temperatures.buffer_upper"),
        (None, "Pole nieużywane", None),
        ("thermoflow-boiler-power", "Moc chwilowa pieca", "boiler_power_kw"),
    ),
    (
        ("thermoflow-valve4d-current", "Temperatura zaworu 4D", "valve.current"),
        (VALVE_SETPOINT_SERIES_ID, VALVE_SETPOINT_SERIES_LABEL, "valve.set"),
        (VALVE_OPENING_SERIES_ID, VALVE_OPENING_SERIES_LABEL, "valve.opening"),
        ("thermoflow-valve4d-return", "Powrót zaworu 4D", "valve.return"),
        ("thermoflow-pump-valve4d", "Pompa zaworu 4D", "pumps.valve4d"),
        ("thermoflow-pump-floor", "Pompa podłogowa", "pumps.floor"),
        ("thermoflow-pump-dhw", "Pompa CWU", "pumps.dhw"),
        ("thermoflow-pump-circulation", "Pompa cyrkulacyjna", "pumps.circulation"),
    ),
    (
        ("thermoflow-co-setpoint", "Temperatura zadana CO", "setpoints.co"),
        ("thermoflow-dhw-setpoint", "Temperatura zadana CWU", "setpoints.dhw"),
        ("thermoflow-fuel-percent", "Zapas paliwa", "fuel.percent"),
        ("thermoflow-room-current", "Temperatura pokoju bieżąca", "temperatures.room"),
        ("thermoflow-valve4d-effective-setpoint", "Temperatura zadana zaworu 4D po korekcie", "valve.set_after_correction"),
        ("thermoflow-valve-hysteresis", "Histereza zaworu 4D", "hysteresis.valve"),
        ("thermoflow-co-hysteresis", "Histereza CO", "hysteresis.boiler"),
        ("thermoflow-dhw-hysteresis", "Histereza CWU", "hysteresis.dhw"),
    ),
)
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
    channel_ids = _thingspeak_settings("THINGSPEAK_CHANNEL_IDS")
    write_keys = _thingspeak_settings("THINGSPEAK_WRITE_KEYS")
    if len(channel_ids) != 3 or len(write_keys) != 3:
        return
    now = time.monotonic()
    data = payload.get("data") or {}
    try:
        created_at = datetime.fromisoformat(payload["fetched_at"]).astimezone(timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError):
        created_at = datetime.now(timezone.utc).isoformat()
    with THINGSPEAK_WRITE_LOCK:
        for index, (channel_id, write_key, fields) in enumerate(zip(channel_ids, write_keys, THINGSPEAK_CHANNEL_FIELDS)):
            if now - THINGSPEAK_LAST_WRITES[index] < THINGSPEAK_WRITE_INTERVAL_SECONDS:
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


def _thingspeak_history(period: str, year: int, month: int) -> tuple[list[dict], list[dict]]:
    """Read configured channel feeds for a local calendar month/year."""
    channel_ids = _thingspeak_settings("THINGSPEAK_CHANNEL_IDS")
    read_keys = _thingspeak_settings("THINGSPEAK_READ_KEYS")
    if len(channel_ids) != 3:
        return [], []
    local_tz = ZoneInfo("Europe/Warsaw")
    start = datetime(year, 1 if period == "year" else month, 1, tzinfo=local_tz)
    if period == "year" or month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=local_tz)
    timescale = 10 if period == "month" else 240
    series_by_id: dict[str, dict] = {}
    pump_states_by_time: dict[str, dict] = {}
    for channel_index, channel_id in enumerate(channel_ids):
        params = {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Europe/Warsaw", "timescale": timescale, "results": 8000}
        if channel_index < len(read_keys) and read_keys[channel_index]:
            params["api_key"] = read_keys[channel_index]
        request = Request(f"https://api.thingspeak.com/channels/{channel_id}/feeds.json?{urlencode(params)}", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=8) as response:
                feeds = json.loads(response.read().decode("utf-8")).get("feeds", [])
        except Exception:
            continue
        for feed in feeds:
            timestamp = feed.get("created_at")
            if not timestamp:
                continue
            for field_number, (series_id, label, _path) in enumerate(THINGSPEAK_CHANNEL_FIELDS[channel_index], start=1):
                if series_id is None:
                    continue
                raw = feed.get(f"field{field_number}")
                value = _number(raw)
                if value is None:
                    continue
                item = series_by_id.setdefault(series_id, {"id": series_id, "label": label, "points": []})
                item["points"].append({"x": timestamp, "y": value})
                if channel_index == 1 and field_number >= 5:
                    pump_key = THINGSPEAK_CHANNEL_FIELDS[channel_index][field_number - 1][2].split(".")[-1]
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
    """Fetch linear temperature history, combining monthly data if yearly range is unsupported."""
    now = time.monotonic()
    cache_key = (udid, period, year, month if period in ("month", "year") else 0)
    with _chart_cache_lock:
        cached = _chart_cache.get(cache_key)
        if cached and now - cached[0] < 300:
            return cached[1]

    base = f"modules/{udid}/statistics/linear/range/"

    def get_month(month_number: int) -> dict:
        return _get(f"{base}month/{month_number}/year/{year}", token)

    if period == "month":
        body = get_month(month)
    else:
        try:
            body = _get(f"{base}{period}", token)
        except HTTPError as error:
            if period != "year" or error.code != 422:
                raise
            # Some controller models document a yearly range but reject it.
            # Assemble the calendar year from the explicit monthly endpoint.
            import concurrent.futures
            month_range = range(1, month + 1) if year == datetime.now().year else range(1, 13)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                months = list(pool.map(get_month, month_range))
            history: dict[str, dict[str, float]] = {}
            descriptions: dict[str, str] = {}
            for monthly in months:
                data = monthly.get("data") or {}
                month_history = data.get("history") or {}
                month_descriptions = data.get("descriptions") or []
                for index, (series_id, points) in enumerate(month_history.items()):
                    if isinstance(month_descriptions, list) and index < len(month_descriptions):
                        descriptions[str(series_id)] = str(month_descriptions[index])
                    target = history.setdefault(str(series_id), {})
                    for point in points if isinstance(points, list) else []:
                        if isinstance(point, dict) and point.get("x") is not None and point.get("y") is not None:
                            target[str(point["x"])] = point["y"]
            body = {"data": {"history": history, "descriptions": descriptions}}

    data = body.get("data") or {}
    history = data.get("history") or {}
    raw_descriptions = data.get("descriptions") or []
    if isinstance(raw_descriptions, list):
        descriptions = {
            str(series_id): str(raw_descriptions[index])
            for index, series_id in enumerate(history)
            if index < len(raw_descriptions)
        }
    elif isinstance(raw_descriptions, dict):
        descriptions = {str(key): str(value) for key, value in raw_descriptions.items()}
    else:
        descriptions = {}

    translations = _translations(token)
    series = []
    for series_id, points in history.items():
        if not isinstance(points, list):
            continue
        description = descriptions.get(str(series_id), str(series_id))
        if description.startswith("|&^4fdkl|"):
            label = description.split("|", 2)[-1]
        else:
            match = re.fullmatch(r"(\d+)(?::(\d+))?", description)
            if match:
                label = STATISTICS_LABELS.get(match.group(1)) or translations.get(
                    match.group(1), STATISTICS_LABELS.get(str(series_id), f"Seria {series_id}")
                )
                if match.group(2):
                    label = f"{label} · {match.group(2)}"
            else:
                label = description or STATISTICS_LABELS.get(str(series_id), f"Seria {series_id}")
        cleaned = []
        for point in points:
            if not isinstance(point, dict) or point.get("x") is None:
                continue
            value = _number(point.get("y"))
            if value is not None:
                cleaned.append({"x": str(point["x"]), "y": value})
        if cleaned:
            series.append({"id": str(series_id), "label": label, "points": cleaned})

    thingspeak_series, pump_states = _thingspeak_history(period, year, month)
    known_series = {item["id"]: item for item in series}
    for item in thingspeak_series:
        target = known_series.get(item["id"])
        if target:
            target["points"].extend(item["points"])
        else:
            series.append(item)
            known_series[item["id"]] = item

    result = {
        "configured": True,
        "connected": True,
        "period": period,
        "year": year,
        "month": month if period == "month" else None,
        "series": series,
        "pump_states": pump_states,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _chart_cache_lock:
        _chart_cache[cache_key] = (now, result)
    return result


def _read_statistics(period: str, year: int | None = None, month: int | None = None) -> dict:
    if period not in ("day", "week", "month", "year"):
        return {"configured": bool(_token()), "connected": False, "error": "Nieobsługiwany zakres statystyk"}
    token = _token()
    if not token:
        return {"configured": False, "connected": False, "error": "Brak tokenu eModul"}
    uid = _user_id(token)
    modules = _get(f"users/{uid}/modules", token)
    if not isinstance(modules, list) or not modules:
        raise RuntimeError("Konto eModul nie zwróciło sterowników")
    wanted = os.getenv("TECH_MODULE_ID", "").strip()
    module = next((item for item in modules if str(item.get("udid", "")) == wanted), None) if wanted else modules[0]
    if module is None or not module.get("udid"):
        raise RuntimeError("TECH_MODULE_ID nie pasuje do sterownika na koncie eModul")
    today = datetime.now()
    selected_year = year or today.year
    selected_month = month or today.month
    if not 2000 <= selected_year <= 2100 or not 1 <= selected_month <= 12:
        return {"configured": True, "connected": False, "error": "Nieprawidłowa data statystyk"}
    return _linear_statistics(str(module["udid"]), token, period, selected_year, selected_month)


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
    for menu_type in ("MU", "MI"):
        try:
            menu_body = _get(f"users/{uid}/modules/{udid}/menu/{menu_type}/", token)
        except Exception:
            continue
        for item in menu_body.get("data", {}).get("elements", []):
            menus.append({
                "menu": menu_type,
                "id": item.get("id"),
                "type": item.get("type"),
                "label": translations.get(str(item.get("txtId", "")), ""),
                "params": item.get("params") or {},
            })

    zones_body = details.get("zones") or {}
    zones = zones_body.get("elements", []) if isinstance(zones_body, dict) else []
    # Statistics endpoints include time-series that are not part of the live
    # tile snapshot (for example the upper buffer sensor). Keep this probe in
    # diagnostics only; the dashboard's regular polling should stay lightweight.
    statistics = {"available": None, "latest": {}, "consumptions_day": None, "consumptions_error": None, "error": None}
    try:
        available = _get(f"modules/{udid}/statistics/available", token)
        linear = _get(f"modules/{udid}/statistics/linear/range/day", token)
        history = (linear.get("data") or {}).get("history") or {}
        statistics["available"] = available
        statistics["latest"] = {
            name: points[-1]
            for name, points in history.items()
            if isinstance(points, list) and points
        }
    except Exception as error:
        statistics["error"] = str(error)[:180]
    # This endpoint is a daily/monthly aggregate. Keep it in diagnostics only;
    # it must not be presented as the controller's instantaneous fuel rate.
    try:
        statistics["consumptions_day"] = _get(f"modules/{udid}/statistics/consumptions/range/day", token)
    except Exception as error:
        statistics["consumptions_error"] = str(error)[:180]
    return {
        "configured": True,
        "connected": True,
        "source": "eModul",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "controller": {
            "name": module.get("name"),
            "type": module.get("type"),
            "version": module.get("version"),
        },
        "counts": {"tiles": len(tiles), "zones": len(zones), "menu_items": len(menus)},
        "tiles": tiles,
        "zones": zones,
        "menus": menus,
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path not in ("/api/status", "/api/health", "/api/modules", "/api/diagnostics", "/api/statistics"):
            self.send_error(404)
            return
        if path == "/api/statistics":
            query = parse_qs(parsed.query)
            try:
                period = query.get("period", ["day"])[0]
                year = int(query.get("year", [str(datetime.now().year)])[0])
                month = int(query.get("month", [str(datetime.now().month)])[0])
                payload = _read_statistics(period, year, month)
            except (HTTPError, URLError, TimeoutError, OSError) as error:
                status = error.code if isinstance(error, HTTPError) else None
                message = f"eModul odrzucił żądanie (HTTP {status})" if status else "Brak połączenia z eModul"
                payload = {"configured": bool(_token()), "connected": False, "error": message}
            except Exception as error:
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180]}
        elif path in ("/api/modules", "/api/diagnostics"):
            try:
                payload = _read_modules() if path == "/api/modules" else _read_diagnostics()
            except HTTPError as error:
                payload = {"configured": True, "connected": False, "error": f"eModul odrzucił żądanie (HTTP {error.code})"}
            except (URLError, TimeoutError, OSError):
                payload = {"configured": bool(_token()), "connected": False, "error": "Brak połączenia z eModul"}
            except Exception as error:
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180]}
        else:
            payload = snapshot()
            if path == "/api/health":
                payload = {key: payload.get(key) for key in ("configured", "connected", "source", "error", "fetched_at")}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        # Do not emit credentials or account-specific API response bodies.
        return


def _poll_forever() -> None:
    """Keep live-derived history recording even when no dashboard tab is open."""
    while True:
        snapshot()
        time.sleep(30)


if __name__ == "__main__":
    threading.Thread(target=_poll_forever, name="eModul-poller", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()
