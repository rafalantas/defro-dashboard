"""Śledzenie zużycia peletu dla defro-dashboard (tylko biblioteka standardowa).

Dosypki są trzymane w osobnym kanale ThingSpeak, agregaty dzienne tylko w pamięci,
nic nie jest zapisywane na dysk. Temperatura zewnętrzna i moc palnika pochodzą
z pierwszego kanału z THINGSPEAK_CHANNEL_IDS, a dla dni sprzed jego utworzenia
temperatura jest brana z archiwum Open-Meteo (FUEL_LAT, FUEL_LON).
Kanał ThingSpeak "Pelet":
  field1 = kg dosypane, field2 = notatka, field3 = entry_id anulowanego wpisu

Endpointy (obsługiwane przez fuel.handle(), wywoływane z api.py):
  GET    /api/fuel/stats?days=30
  GET    /api/fuel/refills
  GET    /api/fuel/summary         podsumowanie do karty w dashboardzie
  POST   /api/fuel/refill          {"kg": 30} lub {"bags": 2}, opcjonalnie "ts", "note"   (X-Api-Token)
  DELETE /api/fuel/refill/<id>     anulowanie wpisu                                         (X-Api-Token)

Założenie: przy każdej dosypce zasobnik jest uzupełniany do tego samego poziomu,
więc ilość dosypana = zużycie od poprzedniej dosypki. Pierwsza dosypka to punkt startowy.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("FUEL_TZ", "Europe/Warsaw"))
API_TOKEN = os.getenv("FUEL_API_TOKEN", "")
TS_API = "https://api.thingspeak.com"

def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    return [item.strip() for item in raw.split(",")] if raw else []


# Kanał 1 (temperatury i moc) z tych samych zmiennych, których używa api.py.
TS_CHANNEL_TEMPS = (_csv_env("THINGSPEAK_CHANNEL_IDS") or [""])[0]
TS_READ_KEY_TEMPS = (_csv_env("THINGSPEAK_READ_KEYS") or [""])[0]
FIELD_OUTDOOR = 4   # thermoflow-outdoor w THINGSPEAK_COMPACT_CHANNEL_FIELDS
FIELD_POWER = 8     # thermoflow-boiler-power
# Pusty = wykrywany automatycznie z daty utworzenia kanału.
TS_DATA_START = os.getenv("FUEL_TEMPS_DATA_START", "").strip()

# Osobny kanał na dosypki.
TS_CHANNEL_FUEL = os.getenv("THINGSPEAK_FUEL_CHANNEL_ID", "").strip()
TS_READ_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_READ_KEY", "").strip()
TS_WRITE_KEY_FUEL = os.getenv("THINGSPEAK_FUEL_WRITE_KEY", "").strip()

FUEL_LAT = os.getenv("FUEL_LAT", "")
FUEL_LON = os.getenv("FUEL_LON", "")
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"

HDD_BASE = float(os.getenv("HDD_BASE", "18.0"))
PELLET_KWH_PER_KG = float(os.getenv("PELLET_KWH_PER_KG", "4.8"))
BOILER_EFF = float(os.getenv("BOILER_EFF", "0.88"))
BAG_KG = float(os.getenv("PELLET_BAG_KG", "15"))
# Pojemność zasobnika: Gamma 40 wg instrukcji ~228 kg (gęstość nasypowa 0,6 kg/dm3).
HOPPER_KG = float(os.getenv("PELLET_HOPPER_KG", "") or 228)
# Zalecany minimalny poziom wg instrukcji: 15 % objętości. Bez czujnika poziomu pusty zasobnik
# kończy się zgaśnięciem płomienia i alarmem nieudanego rozpalenia, więc ostrzegamy przed rezerwą.
HOPPER_RESERVE_PERCENT = float(os.getenv("PELLET_HOPPER_RESERVE_PERCENT", "") or 15)
SUMMARY_CACHE_SECONDS = 60

MAX_GAP_S = 300
MAX_DAYS = 120
MIN_PERIOD_DAYS = 2.0
MAX_PERIODS = 50

_lock = threading.Lock()
_daily_cache: dict[str, dict] = {}
_om_cache: dict[str, float] = {}
_temps_start: list = []   # [date | None] po pierwszym sprawdzeniu
_refills_cache: dict = {"at": 0.0, "data": None}
_summary_cache: dict = {"at": 0.0, "data": None}


class FuelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- HTTP

def _request_json(url: str, params: dict | None = None, form: dict | None = None,
                  payload: dict | None = None, timeout: int = 10):
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {"Accept": "application/json", "User-Agent": "defro-dashboard-fuel"}
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    base = url.split("?")[0]
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        raise FuelError(502, f"{base}: HTTP {e.code}") from None
    except (URLError, TimeoutError, OSError) as e:
        raise FuelError(502, f"{base}: {e}") from None


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _day_bounds(day: date):
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------- temperatury i moc

def _fetch_day(day: date) -> dict:
    start, end = _day_bounds(day)
    params = {
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "results": 8000,
    }
    if TS_READ_KEY_TEMPS:
        params["api_key"] = TS_READ_KEY_TEMPS
    feeds = _request_json(f"{TS_API}/channels/{TS_CHANNEL_TEMPS}/feeds.json", params).get("feeds", [])

    temps, power = [], []
    for f in feeds:
        ts = _parse_ts(f["created_at"]).timestamp()
        t = f.get(f"field{FIELD_OUTDOOR}")
        p = f.get(f"field{FIELD_POWER}")
        if t not in (None, ""):
            temps.append(float(t))
        if p not in (None, ""):
            power.append((ts, float(p)))

    power.sort()
    kwh = 0.0
    for (t0, p0), (t1, _) in zip(power, power[1:]):
        dt = min(t1 - t0, MAX_GAP_S)
        if dt > 0:
            kwh += p0 * dt / 3600.0

    t_mean = sum(temps) / len(temps) if temps else None
    return {
        "day": day.isoformat(),
        "t_mean": round(t_mean, 2) if t_mean is not None else None,
        "hdd": round(max(0.0, HDD_BASE - t_mean), 2) if t_mean is not None else None,
        "burner_kwh": round(kwh, 2),
        "samples": len(feeds),
    }


def _om_fill(url: str, params: dict):
    daily = _request_json(url, params).get("daily", {})
    for t, v in zip(daily.get("time", []), daily.get("temperature_2m_mean", [])):
        if v is not None:
            _om_cache[t] = float(v)


def _openmeteo_mean(day: date):
    key = day.isoformat()
    if key in _om_cache or not (FUEL_LAT and FUEL_LON):
        return _om_cache.get(key)
    today = datetime.now(TZ).date()
    common = {"latitude": FUEL_LAT, "longitude": FUEL_LON,
              "daily": "temperature_2m_mean", "timezone": str(TZ)}
    if day < today - timedelta(days=7):
        start = day.replace(day=1)
        month_end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        end = min(month_end, today - timedelta(days=6))
        _om_fill(OM_ARCHIVE, {**common, "start_date": start.isoformat(), "end_date": end.isoformat()})
    else:
        _om_fill(OM_FORECAST, {**common, "past_days": 14, "forecast_days": 1})
    return _om_cache.get(key)


def _temps_data_start():
    """Pierwszy dzień z danymi w kanale temperatur (z ustawienia albo z daty utworzenia kanału)."""
    if TS_DATA_START:
        return date.fromisoformat(TS_DATA_START)
    if not _temps_start:
        start = None
        if TS_CHANNEL_TEMPS:
            params = {"results": 1}
            if TS_READ_KEY_TEMPS:
                params["api_key"] = TS_READ_KEY_TEMPS
            try:
                channel = _request_json(f"{TS_API}/channels/{TS_CHANNEL_TEMPS}/feeds.json", params).get("channel", {})
                if channel.get("created_at"):
                    start = _parse_ts(channel["created_at"]).astimezone(TZ).date()
            except FuelError:
                return None   # spróbujemy przy następnym zapytaniu
        _temps_start.append(start)
    return _temps_start[0]


def _get_daily(day: date) -> dict:
    key = day.isoformat()
    today = datetime.now(TZ).date()
    if day < today and key in _daily_cache:
        return dict(_daily_cache[key])

    rec = None
    ts_failed = False
    ts_start = _temps_data_start()
    if TS_CHANNEL_TEMPS and (ts_start is None or day >= ts_start):
        try:
            rec = _fetch_day(day)
        except FuelError:
            ts_failed = True   # awaryjnie temperatura z Open-Meteo, bez zapamiętywania

    if rec is None or not rec["samples"] or rec.get("t_mean") is None:
        t = _openmeteo_mean(day)
        rec = {
            "day": key,
            "t_mean": round(t, 2) if t is not None else None,
            "hdd": round(max(0.0, HDD_BASE - t), 2) if t is not None else None,
            "burner_kwh": rec["burner_kwh"] if rec and rec["samples"] else None,
            "samples": rec["samples"] if rec else 0,
            "source": "open-meteo",
        }
    else:
        rec["source"] = "thingspeak"

    if day < today and rec.get("hdd") is not None and not ts_failed:
        _daily_cache[key] = rec
    if ts_failed:
        rec["warning"] = "ThingSpeak niedostępny, temperatura z Open-Meteo"
    return dict(rec)


def _period_sums(start: datetime, end: datetime):
    now = datetime.now(TZ)
    hdd = kwh = 0.0
    hdd_missing = kwh_missing = 0
    d, last = start.astimezone(TZ).date(), end.astimezone(TZ).date()
    while d <= last:
        day_start, day_end = _day_bounds(d)
        overlap = (min(end, day_end) - max(start, day_start)).total_seconds()
        if overlap > 0:
            rec = _get_daily(d)
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

def _refills() -> list[dict]:
    if not TS_CHANNEL_FUEL:
        return []
    cached = _refills_cache["data"]
    if cached is not None and time.monotonic() - _refills_cache["at"] < SUMMARY_CACHE_SECONDS:
        return [dict(r) for r in cached]
    out = _refills_uncached()
    _refills_cache.update(at=time.monotonic(), data=out)
    return [dict(r) for r in out]


def _invalidate_caches():
    _refills_cache.update(at=0.0, data=None)
    _summary_cache.update(at=0.0, data=None)


def _refills_uncached() -> list[dict]:
    params = {"results": 8000}
    if TS_READ_KEY_FUEL:
        params["api_key"] = TS_READ_KEY_FUEL
    feeds = _request_json(f"{TS_API}/channels/{TS_CHANNEL_FUEL}/feeds.json", params).get("feeds", [])
    voided = {int(f["field3"]) for f in feeds if f.get("field3") not in (None, "")}
    out = []
    for f in feeds:
        if f.get("field1") in (None, "") or f["entry_id"] in voided:
            continue
        out.append({
            "id": f["entry_id"],
            "ts": _parse_ts(f["created_at"]).astimezone(TZ).isoformat(),
            "kg": float(f["field1"]),
            "note": f.get("field2") or None,
        })
    out.sort(key=lambda r: r["ts"])
    return out


def _ts_write(fields: dict, created_at: datetime | None = None) -> int:
    if not (TS_CHANNEL_FUEL and TS_WRITE_KEY_FUEL):
        raise FuelError(503, "Kanał ThingSpeak na dosypki nie jest skonfigurowany")
    form = {"api_key": TS_WRITE_KEY_FUEL, **fields}
    if created_at:
        form["created_at"] = created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = _request_json(f"{TS_API}/update.json", form=form)
    entry_id = body.get("entry_id", 0) if isinstance(body, dict) else int(body or 0)
    if not entry_id:
        raise FuelError(429, "ThingSpeak odrzucił zapis (limit 15 s), spróbuj ponownie za chwilę")
    _invalidate_caches()
    return entry_id


def _periods(refills: list[dict]) -> list[dict]:
    refills = refills[-(MAX_PERIODS + 1):]
    out = []
    for prev, cur in zip(refills, refills[1:]):
        start, end = datetime.fromisoformat(prev["ts"]), datetime.fromisoformat(cur["ts"])
        days = (end - start).total_seconds() / 86400
        if days <= 0 or days > MAX_DAYS:
            continue
        hdd, kwh, hdd_missing, kwh_missing = _period_sums(start, end)
        est_raw = kwh / (PELLET_KWH_PER_KG * BOILER_EFF) if not kwh_missing else None
        out.append({
            "start": prev["ts"],
            "end": cur["ts"],
            "days": round(days, 2),
            "kg": cur["kg"],
            "kg_per_day": round(cur["kg"] / days, 2),
            "hdd": round(hdd, 1),
            "kg_per_hdd": round(cur["kg"] / hdd, 3) if hdd > 0 else None,
            "useful_kwh_per_hdd": round(cur["kg"] * PELLET_KWH_PER_KG * BOILER_EFF / hdd, 1) if hdd > 0 else None,
            "burner_kwh": round(kwh, 1) if not kwh_missing else None,
            "est_kg_raw": round(est_raw, 1) if est_raw is not None else None,
            "days_missing_temp": hdd_missing,
            "days_missing_burner": kwh_missing,
        })
    return out


def _calibration(periods: list[dict]) -> dict:
    usable = [p for p in periods
              if p["days"] >= MIN_PERIOD_DAYS and p["est_kg_raw"] and p["days_missing_burner"] == 0]
    actual = sum(p["kg"] for p in usable)
    est = sum(p["est_kg_raw"] for p in usable)
    if not usable or est <= 0:
        return {"factor": 1.0, "calibrated": False, "periods_used": 0}
    return {"factor": round(actual / est, 3), "calibrated": True, "periods_used": len(usable)}


# ---------------------------------------------------------------- akcje

def _stats(query: dict) -> dict:
    try:
        days = max(1, min(int(query.get("days", ["30"])[0]), MAX_DAYS))
    except ValueError:
        raise FuelError(400, "days musi być liczbą") from None

    today = datetime.now(TZ).date()
    errors = []

    def safe_daily(d: date) -> dict:
        try:
            return _get_daily(d)
        except FuelError as e:
            return {"day": d.isoformat(), "error": e.message}

    with _lock:
        wanted = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        # pierwsze wywołanie: niezapamiętane doby pobieramy równolegle (limit nginx to 60 s)
        with ThreadPoolExecutor(max_workers=4) as pool:
            daily = list(pool.map(safe_daily, wanted))
        try:
            refills = _refills()
            periods = _periods(refills)
        except FuelError as e:
            refills, periods = [], []
            errors.append(e.message)

    calib = _calibration(periods)
    for r in daily:
        if r.get("burner_kwh") is None:
            continue
        est = r["burner_kwh"] / (PELLET_KWH_PER_KG * BOILER_EFF) * calib["factor"]
        r["est_kg"] = round(est, 1)
        r["est_kg_per_hdd"] = round(est / r["hdd"], 3) if r.get("hdd") else None

    valid = [r for r in daily if r.get("hdd") is not None and "est_kg" in r]
    tot_hdd = sum(r["hdd"] for r in valid)
    tot_kg = sum(r["est_kg"] for r in valid)
    return {
        "config": {"hdd_base": HDD_BASE, "pellet_kwh_per_kg": PELLET_KWH_PER_KG,
                   "boiler_eff": BOILER_EFF, "bag_kg": BAG_KG},
        "calibration": calib,
        "summary": {"days_with_burner_data": len(valid), "hdd": round(tot_hdd, 1),
                    "est_kg": round(tot_kg, 1),
                    "est_kg_per_hdd": round(tot_kg / tot_hdd, 3) if tot_hdd > 0 else None},
        "refills_count": len(refills),
        "periods": periods,
        "daily": daily,
        "errors": errors,
    }


def _summary() -> dict:
    """Lekkie podsumowanie do karty w dashboardzie (cache 60 s)."""
    cached = _summary_cache["data"]
    if cached is not None and time.monotonic() - _summary_cache["at"] < SUMMARY_CACHE_SECONDS:
        return cached
    if not TS_CHANNEL_FUEL:
        return {"configured": False}

    now = datetime.now(TZ)
    today = now.date()
    with _lock:
        refills = _refills()
        # tylko ostatnie okresy: szybciej, a kalibracja i tak wymaga danych palnika
        periods = _periods(refills[-7:])
        calib = _calibration(periods)
        last7 = [_get_daily(today - timedelta(days=i)) for i in range(7, 0, -1)]

    def est_kg(kwh: float) -> float:
        return kwh / (PELLET_KWH_PER_KG * BOILER_EFF) * calib["factor"]

    # 1) ostatnie 7 pełnych dób: szacunek z mocy palnika (skalibrowany)
    with_burner = [d for d in last7 if d.get("burner_kwh") is not None and d.get("hdd") is not None]
    # pomijamy niepełne doby (np. dzień uruchomienia kanału), żeby nie zaniżały sumy
    if with_burner:
        full = max(d.get("samples", 0) for d in with_burner)
        with_burner = [d for d in with_burner if d.get("samples", 0) >= 0.7 * full]
    week = None
    if with_burner:
        kg = sum(est_kg(d["burner_kwh"]) for d in with_burner)
        hdd = sum(d["hdd"] for d in with_burner)
        week = {
            "days": len(with_burner),
            "kg": round(kg, 1),
            "kg_per_day": round(kg / len(with_burner), 1),
            "hdd": round(hdd, 1),
            "kg_per_hdd": round(kg / hdd, 2) if hdd > 0 else None,
            "t_mean": round(sum(d["t_mean"] for d in with_burner) / len(with_burner), 1),
        }

    # 2) kg na stopniodzień z rzeczywistych dosypek (ostatnie okresy z ok. 30 dni)
    recent = [p for p in periods if p["hdd"] > 0][-4:]
    refills_based = None
    if recent:
        kg = sum(p["kg"] for p in recent)
        hdd = sum(p["hdd"] for p in recent)
        refills_based = {
            "periods": len(recent),
            "from": recent[0]["start"],
            "to": recent[-1]["end"],
            "kg": kg,
            "hdd": round(hdd, 1),
            "kg_per_hdd": round(kg / hdd, 2),
        }

    # 3) od ostatniej dosypki i 4) szacowany stan zasobnika
    last = None
    hopper = None
    if refills:
        lr = refills[-1]
        last_ts = datetime.fromisoformat(lr["ts"])
        hours = (now - last_ts).total_seconds() / 3600
        last = {"ts": lr["ts"], "kg": lr["kg"], "hours_ago": round(hours, 1)}

        with _lock:
            _hdd, kwh, _hm, kwh_missing = _period_sums(last_ts, now)
        if not kwh_missing:
            used, method = est_kg(kwh), "moc palnika skalibrowana do dosypek"
        elif periods:
            used, method = periods[-1]["kg_per_day"] * hours / 24, "średnia z ostatniego okresu"
        else:
            used, method = None, None

        capacity = HOPPER_KG
        reserve = capacity * HOPPER_RESERVE_PERCENT / 100
        if used is not None:
            remaining = max(0.0, capacity - used)
            to_reserve = max(0.0, remaining - reserve)
            rate = (week or {}).get("kg_per_day") or (periods[-1]["kg_per_day"] if periods else None)
            hopper = {
                "capacity_kg": capacity,
                "reserve_kg": round(reserve, 1),
                "used_kg": round(used, 1),
                "remaining_kg": round(remaining, 1),
                "remaining_percent": round(100 * remaining / capacity) if capacity else None,
                "to_reserve_kg": round(to_reserve, 1),
                "below_reserve": remaining <= reserve,
                # czas do rezerwy 15 % (zalecane minimum z instrukcji kotła)
                "days_to_reserve": round(to_reserve / rate, 1) if rate else None,
                "method": method,
            }

    result = {
        "configured": True,
        "calibration": calib,
        "last7": week,
        "refills_based": refills_based,
        "last_refill": last,
        "hopper": hopper,
        "fetched_at": now.isoformat(),
    }
    _summary_cache.update(at=time.monotonic(), data=result)
    return result


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
    try:
        refill_id = int(raw_id)
    except ValueError:
        raise FuelError(400, "Nieprawidłowe id") from None
    if refill_id not in {r["id"] for r in _refills()}:
        raise FuelError(404, "Nie ma takiej dosypki")
    return {"voided": refill_id, "void_entry": _ts_write({"field3": refill_id})}


def _check_token(headers):
    if not API_TOKEN:
        raise FuelError(503, "FUEL_API_TOKEN nie jest ustawiony")
    if not hmac.compare_digest(headers.get("X-Api-Token", ""), API_TOKEN):
        raise FuelError(401, "Brak lub zły token")


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
