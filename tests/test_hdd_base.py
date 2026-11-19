"""Stała temperatura bazowa stopniodni (FUEL_HDD_BASE_FIXED) i zamrożona korekta Open-Meteo, z atrapą usług.

Uruchomienie z katalogu repo: python3 tests/test_hdd_base.py

Sprawdza:
- parsowanie FUEL_HDD_BASE_FIXED (test jawnie ustawia 15,5; puste/„off”/„auto” wyłącza),
- przy stałej bazie wszystkie wskaźniki (model, okresy, doby, podsumowanie, statystyki) liczą się przy 15,5 °C,
  a dopasowanie bazy zostaje informacyjne (best_fit_balance_temp),
- zmiana korekty Open-Meteo o 0,5 K nie zmienia bazy,
- korekta Open-Meteo jest zamrożona od 14 dób z obu źródeł (przeliczana raz w tygodniu), wcześniej co godzinę.
"""
import math
import os
import sys

sys.dont_write_bytecode = True
R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.environ.update(THINGSPEAK_CHANNEL_IDS="1,2", THINGSPEAK_FUEL_CHANNEL_ID="3", FUEL_LAT="52.2",
                  FUEL_LON="21.0", FUEL_API_TOKEN="t")
os.environ["FUEL_HDD_BASE_FIXED"] = "15.5"
sys.path.insert(0, R)
import fuel
from datetime import date, datetime, timedelta, timezone

TZ = fuel.TZ
today = datetime.now(TZ).date()
D0 = today - timedelta(days=80)
TRUE_BASE = 13.0      # prawdziwa temperatura graniczna budynku (inna niż stała 15,5)
om_shift = {"v": 0.0}  # przesunięcie temperatur Open-Meteo (K), zmienia korektę do czujnika sterownika


def truth_t(d):
    return 10 + 9 * math.sin(((d - D0).days) / 9.0)   # 1...19 °C: są doby powyżej i poniżej bazy, więc baza da się odczytać z danych


def truth_kg(d):
    return 5 + 3.4 * max(0.0, TRUE_BASE - truth_t(d))


def cum(ts):
    total, d = 0.0, D0
    while True:
        s, e = fuel._day_bounds(d)
        if s.timestamp() >= ts:
            break
        total += truth_kg(d) * min(1.0, (ts - s.timestamp()) / (e.timestamp() - s.timestamp()))
        d += timedelta(days=1)
    return total


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


REFILLS = []
for k in range(0, 11):
    d = D0 + timedelta(days=7 * k)
    REFILLS.append(datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ))
REFILLS = [t for t in REFILLS if t < datetime.now(TZ)]
calls = {"om": 0}


def fake(url, params=None, form=None, payload=None, timeout=10):
    params = params or {}
    if "open-meteo" in url:
        calls["om"] += 1
        if "start_date" in params:
            a, b = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        else:
            a, b = today - timedelta(days=params.get("past_days", 0)), today + timedelta(days=params.get("forecast_days", 1) - 1)
        days = [a + timedelta(days=k) for k in range((b - a).days + 1)]
        return {"daily": {"time": [d.isoformat() for d in days],
                          "temperature_2m_mean": [round(truth_t(d) - 0.8 + om_shift["v"], 3) for d in days],
                          "shortwave_radiation_sum": [3.0 for _ in days], "wind_speed_10m_max": [20.0 for _ in days]}}
    if "/channels/3/feeds.json" in url:
        feeds = [{"entry_id": i + 1, "created_at": iso(t.timestamp()),
                  "field1": str(round(cum(t.timestamp()) - cum(REFILLS[i - 1].timestamp()), 1) if i else 240.0),
                  "field2": None, "field3": None} for i, t in enumerate(REFILLS)]
        return {"feeds": feeds}
    if "/channels/1/feeds.json" in url:
        if params.get("results") == 1:
            return {"channel": {"created_at": iso(datetime.combine(D0, datetime.min.time(), tzinfo=TZ).timestamp())}, "feeds": []}
        s, e = parse(params["start"]), parse(params["end"])
        t = truth_t(datetime.fromtimestamp(s + 3600, TZ).date())
        now = datetime.now(timezone.utc).timestamp()
        return {"feeds": [{"created_at": iso(x), "field4": str(round(t, 2)), "field8": "6.0"}
                          for x in range(int(s), int(min(e, now)), 60)]}
    if "/channels/2/fields/8.json" in url:
        return {"feeds": []}
    raise AssertionError("nieoczekiwane URL " + url)


fuel._request_json = fake
# Test HDD-base behavior independently; telemetry aggregation has its own regression suite.
fuel._telemetry_history = lambda days: {"daily": []}


def reset(keep_offset=False):
    caches = [fuel._refills_cache, fuel._summary_cache, fuel._forecast_cache]
    if not keep_offset:
        caches.append(fuel._offset_cache)
    for cache in caches:
        cache.update(at=0.0, data=None)
    fuel._model_cache.update(key=None, data=None)
    fuel._daily_model_cache.update(key=None, data=None)
    fuel._summary_state.update(thread=None, error=None)
    fuel._om_cache.clear()
    fuel._daily_cache.clear()


def snapshot():
    """Baza widziana w każdym miejscu, które ją pokazuje albo z niej liczy."""
    stats = fuel._stats({"days": ["30"]})
    summ = fuel._summary()
    model = stats["model"]
    return {
        "active": fuel._active_base(),
        "model": model["balance_temp"],
        "best_fit": model["best_fit_balance_temp"],
        "stats_config": stats["config"]["hdd_base"],
        "summary": summ["hdd_base"],
        "periods": {p["hdd_base"] for p in stats["periods"]},
        "daily": {r["hdd_base"] for r in stats["daily"] if "hdd_base" in r},
        "offset": stats["om_offset"],
        "kg_per_hdd": model["kg_per_hdd"], "base_kg": model["base_kg_per_day"],
        "stats": stats, "summ": summ,
    }


# ---------------------------------------------------------------- parsowanie
parse_cases = {"": None, "16": 16.0, "15,5": 15.5, "14.25": 14.25, "off": None, "AUTO": None, "0": None, "none": None, "bzdura": None}
for raw, want in parse_cases.items():
    assert fuel._parse_hdd_base_fixed(raw) == want, (raw, fuel._parse_hdd_base_fixed(raw), want)
assert fuel.HDD_BASE_FIXED == 15.5
print("parsowanie FUEL_HDD_BASE_FIXED (pusta/nieprawidłowa wartość i off/auto wyłączają): OK")

# ---------------------------------------------------------------- stała baza, korekta Open-Meteo zmieniona o 0,5 K
reset()
a = snapshot()
print("\nstała baza 15,5: model %s, dopasowanie informacyjnie %s, korekta OM %+.2f K (%d dób, zamrożona=%s)" % (
    a["model"], a["best_fit"], a["offset"]["offset"], a["offset"]["days"], a["offset"]["frozen"]))
assert a["active"] == a["model"] == a["stats_config"] == a["summary"] == 15.5
assert a["periods"] == {15.5} and a["daily"] == {15.5}, (a["periods"], a["daily"])
assert a["best_fit"] in fuel.MODEL_BASES and abs(a["best_fit"] - TRUE_BASE) <= 0.5, a["best_fit"]   # informacyjnie widać prawdziwą bazę
assert a["summ"]["model"]["balance_temp"] == 15.5 and a["summ"]["model"]["base_fixed"] is True
assert a["summ"]["model"]["best_fit_balance_temp"] == a["best_fit"]
assert a["summ"]["hdd_base_fixed"] is True and a["stats"]["config"]["hdd_base_fixed"] == 15.5
assert all(f["day"] for f in a["summ"]["forecast"]) and a["summ"]["model"]["periods_used"] >= 4

om_shift["v"] = 0.5     # Open-Meteo cieplejsze o 0,5 K, więc korekta do czujnika sterownika spada o 0,5 K
reset()
b = snapshot()
delta = b["offset"]["offset"] - a["offset"]["offset"]
print("po zmianie Open-Meteo o +0,5 K: korekta %+.2f K (zmiana %+.2f K), baza: aktywna %s, model %s, podsumowanie %s, statystyki %s" % (
    b["offset"]["offset"], delta, b["active"], b["model"], b["summary"], b["stats_config"]))
assert abs(delta + 0.5) < 0.05, delta   # korekta naprawdę się zmieniła o 0,5 K
assert b["active"] == b["model"] == b["stats_config"] == b["summary"] == 15.5   # a baza została
assert b["periods"] == {15.5} and b["daily"] == {15.5}
assert b["summ"]["model"]["balance_temp"] == 15.5
print("zmiana korekty o 0,5 K nie zmienia bazy: OK")

# ---------------------------------------------------------------- bez stałej bazy: baza dopasowana (zachowanie dotychczasowe)
fuel.HDD_BASE_FIXED = None
om_shift["v"] = 0.0
reset()
c = snapshot()
print("\nbez stałej bazy: model %s (dopasowanie %s), okresy %s" % (c["model"], c["best_fit"], c["periods"]))
assert c["model"] == c["best_fit"] == c["active"] == c["summary"] and abs(c["model"] - TRUE_BASE) <= 0.5
assert c["periods"] == {c["model"]} and c["summ"]["model"]["base_fixed"] is False and c["summ"]["hdd_base_fixed"] is False
fuel.HDD_BASE_FIXED = 15.5
print("wyłączenie stałej bazy wraca do dopasowywanej: OK")

# ---------------------------------------------------------------- zamrożenie korekty Open-Meteo
om_shift["v"] = 0.0
reset()
off1 = fuel._om_offset()
assert off1["days"] >= 14 and off1["frozen"] is True and off1["applied"], off1
print("\nkorekta z %d dób: %+.2f K, zamrożona" % (off1["days"], off1["offset"]))
om_shift["v"] = 0.5
fuel._om_cache.clear()           # Open-Meteo zwróciłoby teraz inne wartości...
fuel._daily_cache.clear()
fuel._offset_cache["at"] -= 2 * 3600     # ...a od ostatniego liczenia minęły 2 godziny (poprzednio tyle wystarczało do przeliczenia)
before = calls["om"]
off2 = fuel._om_offset()
assert off2 is off1 and off2["offset"] == off1["offset"] and calls["om"] == before, (off2, off1)
fuel._offset_cache["at"] -= 6 * 24 * 3600   # razem 6 dni 2 h: nadal zamrożona
assert fuel._om_offset()["offset"] == off1["offset"] and calls["om"] == before
print("po 2 godzinach i po 6 dniach korekta bez zmian i bez zapytań do Open-Meteo: OK")
fuel._offset_cache["at"] -= 2 * 24 * 3600   # razem ponad 8 dni: przeliczenie raz w tygodniu
off3 = fuel._om_offset()
assert abs(off3["offset"] - (off1["offset"] - 0.5)) < 0.05 and off3["frozen"] and calls["om"] > before, (off3, off1)
print("po tygodniu korekta przeliczona: %+.2f K -> %+.2f K: OK" % (off1["offset"], off3["offset"]))

# krótka historia: poniżej 14 dób korekta nie jest zamrożona i przelicza się co godzinę
fuel.TS_DATA_START = (today - timedelta(days=10)).isoformat()
om_shift["v"] = 0.0
reset()
short1 = fuel._om_offset()
assert short1["days"] == 10 and short1["frozen"] is False and short1["applied"], short1
om_shift["v"] = 0.5
fuel._om_cache.clear()
fuel._daily_cache.clear()
fuel._offset_cache["at"] -= 2 * 3600
short2 = fuel._om_offset()
assert abs(short2["offset"] - (short1["offset"] - 0.5)) < 0.05, (short1, short2)
print("poniżej 14 dób korekta nie jest zamrożona i przelicza się po godzinie: %+.2f K -> %+.2f K: OK" % (short1["offset"], short2["offset"]))
fuel.TS_DATA_START = ""
print("\nHDD BASE OK")
