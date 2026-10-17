"""Read-only TECH/eModul data bridge for the ThermoFlow dashboard."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://emodul.eu/api/v1/"
POLL_CACHE_SECONDS = 20
STATISTICS_CACHE_SECONDS = 90
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
_statistics_cache: dict[str, tuple[float, float | None, str | None]] = {}
_statistics_lock = threading.Lock()
_chart_cache: dict[tuple[str, str, int, int], tuple[float, dict]] = {}
_chart_cache_lock = threading.Lock()
_i18n_cache: dict[str, str] | None = None
_history_lock = threading.Lock()
VALVE_OPENING_SERIES_ID = "thermoflow-valve4d-opening"
VALVE_OPENING_SERIES_LABEL = "Otwarcie zaworu 4D"


def _token() -> str:
    token_file = os.getenv("TECH_API_TOKEN_FILE")
    if token_file:
        with open(token_file, encoding="utf-8") as stream:
            return stream.read().strip()
    return os.getenv("TECH_API_TOKEN", "").strip()


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


def _buffer_upper_temperature(udid: str, token: str) -> tuple[float | None, str | None]:
    """Read the newest C1 / upper-buffer value from today's statistics."""
    now = time.monotonic()
    with _statistics_lock:
        cached = _statistics_cache.get(udid)
        if cached and now - cached[0] < STATISTICS_CACHE_SECONDS:
            return cached[1], cached[2]

    value = None
    sampled_at = None
    try:
        body = _get(f"modules/{udid}/statistics/linear/range/day", token)
        history = (body.get("data") or {}).get("history") or {}
        points = history.get(BUFFER_UPPER_SERIES_ID) or []
        points = [point for point in points if isinstance(point, dict) and point.get("y") is not None]
        if points:
            latest = max(points, key=lambda point: str(point.get("x", "")))
            value = _number(latest.get("y"))
            sampled_at = str(latest.get("x", "")) or None
    except Exception:
        # Statistics are supplementary; a chart endpoint failure must not
        # interrupt the ordinary live telemetry.
        pass

    with _statistics_lock:
        _statistics_cache[udid] = (now, value, sampled_at)
    return value, sampled_at


def _history_connection():
    path = os.getenv("THERMOFLOW_HISTORY_DB", "/data/thermoflow.sqlite3")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return sqlite3.connect(path, timeout=10)


def _record_valve_opening(payload: dict) -> None:
    """Persist the live 4D valve opening because eModul omits it from history."""
    data = payload.get("data") or {}
    valve = data.get("valve") or {}
    opening = _number(valve.get("opening"))
    if opening is None:
        return
    try:
        sampled_at = datetime.fromisoformat(payload["fetched_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        sampled_at = time.time()
    try:
        with _history_lock, _history_connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS valve_opening (sampled_at REAL NOT NULL, value REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO valve_opening (sampled_at, value) VALUES (?, ?)",
                (sampled_at, opening),
            )
    except (OSError, sqlite3.Error):
        # Dashboard status remains available even if local history storage fails.
        return


def _valve_opening_points(period: str, year: int, month: int) -> list[dict]:
    local_tz = ZoneInfo("Europe/Warsaw")
    start = datetime(year, 1 if period == "year" else month, 1, tzinfo=local_tz)
    if period == "year":
        end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    elif month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=local_tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=local_tz)
    try:
        with _history_lock, _history_connection() as connection:
            rows = connection.execute(
                "SELECT sampled_at, value FROM valve_opening "
                "WHERE sampled_at >= ? AND sampled_at < ? ORDER BY sampled_at",
                (start.timestamp(), end.timestamp()),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    return [
        {"x": datetime.fromtimestamp(sampled_at, timezone.utc).isoformat(), "y": value}
        for sampled_at, value in rows
    ]


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

    opening_points = _valve_opening_points(period, year, month)
    if opening_points:
        series.append({
            "id": VALVE_OPENING_SERIES_ID,
            "label": VALVE_OPENING_SERIES_LABEL,
            "points": opening_points,
        })

    result = {
        "configured": True,
        "connected": True,
        "period": period,
        "year": year,
        "month": month if period == "month" else None,
        "series": series,
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
                    if widget_label and widget_value is not None:
                        data["other_tiles"].append({
                            "label": widget_label,
                            "value": widget_value,
                            "unit": "°C",
                        })

        if kind == 1:
            value = _number(params.get("value"), 10)
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
            elif label:
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
            data["fuel"] = {
                "percent": params.get("percentage"),
                "hours": hours if isinstance(hours, (int, float)) and hours >= 0 else None,
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

    buffer_upper, buffer_upper_at = _buffer_upper_temperature(udid, token)
    data["temperatures"]["buffer_upper"] = buffer_upper
    data["temperatures"]["buffer_upper_at"] = buffer_upper_at

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
    unit_divisors = {4: 10, 5: 100, 7: 10, 23: 10, 26: 10, 33: 10}

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
    statistics = {"available": None, "latest": {}, "error": None}
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
                _record_valve_opening(payload)
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
