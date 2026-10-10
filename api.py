"""Read-only TECH/eModul data bridge for the ThermoFlow dashboard."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://emodul.eu/api/v1/"
POLL_CACHE_SECONDS = 20
_cache: dict = {"updated": 0.0, "payload": None}
_cache_lock = threading.Lock()
_i18n_cache: dict[str, str] | None = None


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
    return translations.get(str(params.get(key, "")), "")


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
        "temperatures": {"co": None, "dhw": None, "flue": None, "outdoor": None},
        "pumps": {"floor": None, "radiator": None, "dhw": None, "circulation": None},
        "valve": {"current": None, "return": None, "set": None, "opening": None},
        "fuel": {"percent": None, "hours": None},
        "fire": None,
        "fan": {"running": None, "gear": None, "rpm": None},
        "disinfection": {"active": None, "percent": None},
        "mode": None,
        "controller_status": None,
        "setpoints": {"co": None, "dhw": None},
        "other_tiles": [],
    }
    for tile in details.get("tiles", []):
        params = tile.get("params") or {}
        kind = tile.get("type")
        label = _label(params, translations)
        normalized = label.casefold()

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
            elif "powrot" in normalized:
                data["temperatures"]["return"] = value
            elif label:
                data["other_tiles"].append({"label": label, "value": value, "unit": "°C"})
        elif kind == 11:
            running = bool(params.get("workingStatus"))
            if "pompa co" in normalized:
                data["pumps"]["radiator"] = running
            elif "pompa cwu" in normalized or "pompa c.w.u" in normalized:
                data["pumps"]["dhw"] = running
            elif "podajnik" in normalized:
                data.setdefault("outputs", {})["feeder"] = running
            elif "grzał" in normalized or "grzal" in normalized:
                data.setdefault("outputs", {})["heater"] = running
            elif label:
                data.setdefault("outputs", {})[label] = running
        elif kind == 21:
            pump_name = _label(params, translations)
            if "cyrkul" in pump_name.casefold():
                data["pumps"]["circulation"] = bool(params.get("workingStatus"))
            else:
                data["pumps"]["additional"] = bool(params.get("workingStatus"))
        elif kind == 22:
            data["fan"] = {
                "running": bool(params.get("workingStatus")),
                "gear": params.get("gear"),
                "rpm": params.get("rpm", params.get("speed")),
            }
        elif kind == 23:
            data["valve"] = {
                "current": _number(params.get("currentTemp"), 10),
                "return": _number(params.get("returnTemp"), 10),
                "set": _number(params.get("setTemp")),
                "opening": params.get("openingPercentage"),
                "working": bool(params.get("workingStatus")),
            }
        elif kind == 31:
            data["fuel"] = {"percent": params.get("percentage"), "hours": params.get("hours")}
        elif kind == 2:
            data["fire"] = bool(params.get("value"))
        elif kind == 32:
            data["disinfection"] = {
                "active": bool(params.get("percentage")),
                "percent": params.get("percentage"),
            }
        elif kind == 40:
            header = translations.get(str(params.get("headerId", "")), "")
            status = translations.get(str(params.get("statusId", "")), "")
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

    # User/installer menus are read-only here. They can expose boiler/DHW
    # setpoints that are not included in the dashboard temperature tiles.
    for menu_type in ("MU", "MI"):
        try:
            menu_body = _get(f"users/{uid}/modules/{udid}/menu/{menu_type}/", token)
        except Exception:
            continue
        for item in menu_body.get("data", {}).get("elements", []):
            params = item.get("params") or {}
            label = translations.get(str(item.get("txtId", "")), "")
            normalized = re.sub(r"\s+", " ", label.casefold().replace(".", ""))
            raw_value = params.get("value")
            value = _number(raw_value, 10 if params.get("format") == 2 else 1)
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


def snapshot() -> dict:
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and now - _cache["updated"] < POLL_CACHE_SECONDS:
            return _cache["payload"]
        try:
            payload = _read_snapshot()
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
        if self.path not in ("/api/status", "/api/health", "/api/modules"):
            self.send_error(404)
            return
        if self.path == "/api/modules":
            try:
                payload = _read_modules()
            except HTTPError as error:
                payload = {"configured": True, "connected": False, "error": f"eModul odrzucił żądanie (HTTP {error.code})", "modules": []}
            except (URLError, TimeoutError, OSError):
                payload = {"configured": bool(_token()), "connected": False, "error": "Brak połączenia z eModul", "modules": []}
            except Exception as error:
                payload = {"configured": bool(_token()), "connected": False, "error": str(error)[:180], "modules": []}
        else:
            payload = snapshot()
            if self.path == "/api/health":
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


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server.serve_forever()
