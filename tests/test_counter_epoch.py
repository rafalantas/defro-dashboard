"""Kalibracja licznika przed i po zmianie podajnika (FUEL_COUNTER_EPOCH), z atrapą ThingSpeak i Open-Meteo.

Uruchomienie z katalogu repo: python3 tests/test_counter_epoch.py
Scenariusze: brak dosypki po epoce, jedna (okres obejmuje epokę), jeden i dwa pełne okresy po epoce.
"""
import math
import os
import sys

sys.dont_write_bytecode = True
R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.environ.update(THINGSPEAK_CHANNEL_IDS="1,2", THINGSPEAK_FUEL_CHANNEL_ID="3", FUEL_LAT="52.2",
                  FUEL_LON="21.0", FUEL_API_TOKEN="t")
sys.path.insert(0, R)
import fuel
from datetime import date, datetime, timedelta, timezone

TZ = fuel.TZ
today = datetime.now(TZ).date()
D0 = today - timedelta(days=80)
EPOCH = datetime(today.year, today.month, today.day, 18, 27, tzinfo=TZ) - timedelta(days=20)
fuel.COUNTER_EPOCH = EPOCH
KPP_OLD, KPP_NEW = 3.2, 2.4


def truth(d):
    i = (d - D0).days
    t = 3 + 7 * math.sin(i / 9.0)
    solar = 3 + 8 * (1 + math.sin(i / 3.3)) / 2
    wind = 10 + 25 * (1 + math.sin(i / 2.1 + 1)) / 2
    hdd = max(0.0, 14.0 - t)
    return t, solar, wind, max(5 + 3.4 * hdd - 0.9 * solar, 2.0)


def cum(ts):
    total, d = 0.0, D0
    while True:
        s, e = fuel._day_bounds(d)
        if s.timestamp() >= ts:
            break
        total += truth(d)[3] * min(1.0, (ts - s.timestamp()) / (e.timestamp() - s.timestamp()))
        d += timedelta(days=1)
    return total


def pct(ts):   # skumulowany spadek licznika w pp: inny współczynnik po epoce
    e = EPOCH.timestamp()
    return cum(min(ts, e)) / KPP_OLD + max(0.0, cum(ts) - cum(e)) / KPP_NEW


def at(day_offset):
    d = today - timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ)


refill_times = []   # ustawiane per scenariusz


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def fake(url, params=None, form=None, payload=None, timeout=10):
    params = params or {}
    if "open-meteo" in url:
        if "start_date" in params:
            a, b = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
        else:
            a, b = today - timedelta(days=params.get("past_days", 0)), today + timedelta(days=params.get("forecast_days", 1) - 1)
        days = [a + timedelta(days=k) for k in range((b - a).days + 1)]
        return {"daily": {"time": [d.isoformat() for d in days],
                          "temperature_2m_mean": [round(truth(d)[0] - 0.8, 2) for d in days],
                          "shortwave_radiation_sum": [round(truth(d)[1], 2) for d in days],
                          "wind_speed_10m_max": [round(truth(d)[2], 1) for d in days]}}
    if "/channels/3/feeds.json" in url:
        feeds = []
        for i, t in enumerate(refill_times):
            kg = cum(t.timestamp()) - cum(refill_times[i - 1].timestamp()) if i else 240.0
            feeds.append({"entry_id": i + 1, "created_at": iso(t.timestamp()), "field1": str(round(kg, 1)),
                          "field2": None, "field3": None})
        return {"feeds": feeds}
    if "/channels/1/feeds.json" in url:
        if params.get("results") == 1:
            return {"channel": {"created_at": iso(datetime.combine(D0, datetime.min.time(), tzinfo=TZ).timestamp())}, "feeds": []}
        s, e = parse(params["start"]), parse(params["end"])
        t = truth(datetime.fromtimestamp(s + 3600, TZ).date())[0]
        now = datetime.now(timezone.utc).timestamp()
        return {"feeds": [{"created_at": iso(x), "field4": str(round(t, 2)), "field8": "6.0"}
                          for x in range(int(s), int(min(e, now)), 60)]}
    if "/channels/2/fields/8.json" in url:
        s, e = parse(params["start"]), parse(params["end"])
        now = datetime.now(timezone.utc).timestamp()
        return {"feeds": [{"created_at": iso(x), "field8": str(round(100 - pct(x) % 70, 3))}
                          for x in range(int(s), int(min(e, now)), 600)]}
    raise AssertionError("nieoczekiwane URL " + url)


fuel._request_json = fake


def reset(refills):
    global refill_times
    refill_times = refills
    for cache in (fuel._refills_cache, fuel._summary_cache, fuel._offset_cache, fuel._forecast_cache):
        cache.update(at=0.0, data=None)
    fuel._model_cache.update(key=None, data=None)
    fuel._daily_model_cache.update(key=None, data=None)
    fuel._summary_state.update(thread=None, error=None)


PRE = [at(80 - 7 * k) for k in range(0, 9)]   # co 7 dni od D0 do today-24 (przed epoką: today-20 18:27)
assert PRE[-1] < EPOCH
SCENARIOS = {
    "A: brak dosypki po epoce": PRE,
    "B: jedna dosypka po epoce (okres obejmuje epokę)": PRE + [at(17)],
    "C: jeden pełny okres po epoce": PRE + [at(17), at(10)],
    "D: dwa pełne okresy po epoce": PRE + [at(17), at(10), at(3)],
}

# --- parsowanie FUEL_COUNTER_EPOCH
assert fuel._parse_epoch("") == datetime.fromisoformat("2026-11-13T18:27:00+01:00")
assert fuel._parse_epoch("2026-11-13T18:27:00") == datetime(2026, 11, 13, 18, 27, tzinfo=TZ)
assert fuel._parse_epoch("bzdura") == datetime.fromisoformat("2026-11-13T18:27:00+01:00")
assert fuel._parse_epoch("2026-12-01T08:00:00+00:00").utcoffset() == timedelta(0)
print("parsowanie epoki: OK")

for name, refills in SCENARIOS.items():
    print("\n== " + name)
    reset(refills)
    daily = fuel._daily({"days": ["70"]})
    cal = daily["counter_calibration"]
    old, new = cal["old"], cal["new"]
    print("stary: %s (%s okr.)  nowy: %s (%s okr.)  collecting=%s" % (old["kg_per_percent"], old["periods_used"], new["kg_per_percent"], new["periods_used"], cal["collecting"]))
    assert old["calibrated"] and abs(old["kg_per_percent"] - KPP_OLD) < 0.05, old
    full_new = sum(1 for p, c in zip(refills, refills[1:]) if p >= EPOCH)
    if full_new == 0:
        assert not new["calibrated"] and cal["collecting"] and cal["message"] == fuel.COUNTER_COLLECTING_MESSAGE
        assert cal["calibrated"] is False and cal["kg_per_percent"] is None   # teraz obowiązuje nowy, jeszcze nieznany
    else:
        assert new["calibrated"] and abs(new["kg_per_percent"] - KPP_NEW) < 0.05, new
        assert not cal["collecting"] and abs(cal["kg_per_percent"] - KPP_NEW) < 0.05
    rows = {r["day"]: r for r in daily["days"]}
    epoch_day = EPOCH.date().isoformat()
    errs_old, errs_new = [], []
    for day, r in rows.items():
        d = date.fromisoformat(day)
        if r["counter_regime"] == "epoch":
            assert day == epoch_day and r["kg_counter"] is None, r
        elif r["counter_regime"] == "old":
            if r["kg_counter"] is not None:
                errs_old.append(abs(r["kg_counter"] - truth(d)[3]))
        else:
            if full_new == 0:
                assert r["kg_counter"] is None, (day, r)
            elif r["kg_counter"] is not None:
                errs_new.append(abs(r["kg_counter"] - truth(d)[3]))
    assert errs_old and max(errs_old) < 0.5, errs_old[:5]
    print("doby przed epoką: %d (maks. błąd %.2f kg)" % (len(errs_old), max(errs_old)))
    if full_new:
        assert errs_new and max(errs_new) < 0.5, errs_new[:5]
        print("doby po epoce: %d (maks. błąd %.2f kg)" % (len(errs_new), max(errs_new)))
    else:
        print("doby po epoce z licznikiem: 0 (zbieranie danych)")
    dm = daily["daily_model"]
    print("model dobowy:", dm["status"], dm.get("terms"), "dób:", dm["days"])

    # przedział obejmujący epokę: starym przed, nowym po
    a, b = EPOCH - timedelta(days=2), EPOCH + timedelta(days=2)
    kg, ok, _ = fuel._counter_kg(a, b, cal)
    if full_new:
        want = cum(b.timestamp()) - cum(a.timestamp())
        assert ok and abs(kg - want) < 1.0, (kg, want)
        print("przedział przez epokę: %.1f kg (prawda %.1f)" % (kg, want))
    else:
        assert not ok and kg is None
        print("przedział przez epokę: brak (nowy współczynnik nieznany)")
    kg0, ok0, _ = fuel._counter_kg(EPOCH - timedelta(days=5), EPOCH - timedelta(days=1), cal)
    assert ok0 and abs(kg0 - (cum((EPOCH - timedelta(days=1)).timestamp()) - cum((EPOCH - timedelta(days=5)).timestamp()))) < 1.0

    # karta (podsumowanie)
    summ = fuel._summary()
    cc = summ["counter_calibration"]
    hop = summ["hopper"]
    print("karta: collecting=%s | tydzień: %s | zasobnik: %s | notatka: %s" % (
        cc["collecting"], (summ["last7"] or {}).get("source"), hop["method"], hop["counter_note"]))
    if full_new == 0:
        assert cc["collecting"] and cc["message"] == fuel.COUNTER_COLLECTING_MESSAGE
        assert hop["used_kg_counter"] is None and hop["counter_note"] == fuel.COUNTER_COLLECTING_MESSAGE
        assert (summ["last7"] or {}).get("source") != "licznik podajnika"
        assert "licznik" not in hop["method"]
    else:
        assert not cc["collecting"] and hop["counter_note"] is None
        assert hop["used_kg_counter"] is not None and (summ["last7"] or {}).get("source") == "licznik podajnika"
        # zużycie od ostatniej dosypki z licznika ≈ prawda
        want = cum(datetime.now(TZ).timestamp()) - cum(refills[-1].timestamp())
        assert abs(hop["used_kg_counter"] - want) < 1.5, (hop["used_kg_counter"], want)

    # okresy: reżim licznika
    st = fuel._stats({"days": ["5"]})
    regimes = [p["counter_regime"] for p in st["periods"]]
    print("okresy:", regimes)
    assert regimes.count("epoch") == (1 if len(refills) > len(PRE) else 0)
print("\nEPOCH OK")
