"""Kalibracja licznika przed i po zmianie podajnika (FUEL_COUNTER_EPOCH), z atrapą ThingSpeak i Open-Meteo.

Uruchomienie z katalogu repo: python3 tests/test_counter_epoch.py
Scenariusze: brak dosypki po epoce, jedna (okres obejmuje epokę), jeden i dwa pełne okresy po epoce.
Po epoce, do pierwszego pełnego okresu, licznik używa współczynnika tymczasowego (HOPPER_KG/100),
a moc palnika oczekiwanego BURNER_FACTOR_AFTER_EPOCH; oba są kalibrowane osobno przed i po epoce.
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
F_OLD, F_NEW = 1.7, 0.80   # współczynnik mocy palnika przed i po epoce (po epoce inny niż oczekiwane 0,94)


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

        def power(x):   # kW tak, żeby kWh/(4,8*0,88)*F dawało prawdziwe kg danej doby
            kg = truth(datetime.fromtimestamp(x, TZ).date())[3]
            return kg * fuel.PELLET_KWH_PER_KG * fuel.BOILER_EFF / 24 / (F_OLD if x < EPOCH.timestamp() else F_NEW)

        return {"feeds": [{"created_at": iso(x), "field4": str(round(t, 2)), "field8": str(round(power(x), 4))}
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
    PROV = fuel.COUNTER_PROVISIONAL_KG_PER_PERCENT
    assert abs(PROV - 2.28) < 1e-9, PROV
    if full_new == 0:
        assert not new["calibrated"] and cal["collecting"] and cal["provisional"]
        assert cal["message"].startswith(fuel.COUNTER_COLLECTING_MESSAGE) and "2,28 kg/%" in cal["message"] and "szacunek" in cal["message"], cal["message"]
        assert cal["calibrated"] is False and cal["kg_per_percent"] == 2.28   # tymczasowo cały zasobnik = 100 %
    else:
        assert new["calibrated"] and abs(new["kg_per_percent"] - KPP_NEW) < 0.05, new
        assert not cal["collecting"] and abs(cal["kg_per_percent"] - KPP_NEW) < 0.05
    rows = {r["day"]: r for r in daily["days"]}
    epoch_day = EPOCH.date().isoformat()
    errs_old, errs_new, errs_prov = [], [], []
    for day, r in rows.items():
        d = date.fromisoformat(day)
        if r["counter_regime"] == "epoch":
            assert day == epoch_day and r["kg_counter"] is None, r
        elif r["counter_regime"] == "old":
            if r["kg_counter"] is not None:
                errs_old.append(abs(r["kg_counter"] - truth(d)[3]))
        else:
            if full_new == 0:
                assert r["estimate"] is True and r["kg_counter"] is not None, (day, r)
                errs_prov.append(abs(r["kg_counter"] - truth(d)[3] * PROV / KPP_NEW))   # drop*2,28 = kg*2,28/2,4
            elif r["kg_counter"] is not None:
                assert r["estimate"] is False
                errs_new.append(abs(r["kg_counter"] - truth(d)[3]))
    assert errs_old and max(errs_old) < 0.5, errs_old[:5]
    print("doby przed epoką: %d (maks. błąd %.2f kg)" % (len(errs_old), max(errs_old)))
    if full_new:
        assert errs_new and max(errs_new) < 0.5, errs_new[:5]
        print("doby po epoce: %d (maks. błąd %.2f kg)" % (len(errs_new), max(errs_new)))
    else:
        assert errs_prov and max(errs_prov) < 0.3, errs_prov[:5]
        print("doby po epoce ze współczynnikiem tymczasowym (szacunek): %d (zgodne z 2,28 kg/%%, prawdziwe zużycie jest %.0f%% wyższe)" % (len(errs_prov), (KPP_NEW / PROV - 1) * 100))
    dm = daily["daily_model"]
    print("model dobowy:", dm["status"], dm.get("terms"), "dób:", dm["days"])
    n_exact = len(errs_old) + len(errs_new)
    assert dm["days"] == n_exact, (dm["days"], n_exact)   # doby-szacunki nie uczą modelu

    # przedział obejmujący epokę: starym przed, nowym po
    a, b = EPOCH - timedelta(days=2), EPOCH + timedelta(days=2)
    kg, ok, _ = fuel._counter_kg(a, b, cal)
    want = cum(b.timestamp()) - cum(a.timestamp())
    if full_new:
        assert ok and abs(kg - want) < 1.0, (kg, want)
        print("przedział przez epokę: %.1f kg (prawda %.1f)" % (kg, want))
    else:
        want_prov = (cum(EPOCH.timestamp()) - cum(a.timestamp())) + (cum(b.timestamp()) - cum(EPOCH.timestamp())) * PROV / KPP_NEW
        assert ok and abs(kg - want_prov) < 1.0, (kg, want_prov)
        print("przedział przez epokę: %.1f kg (szacunek z 2,28 po epoce; prawda %.1f)" % (kg, want))
    kg0, ok0, _ = fuel._counter_kg(EPOCH - timedelta(days=5), EPOCH - timedelta(days=1), cal)
    assert ok0 and abs(kg0 - (cum((EPOCH - timedelta(days=1)).timestamp()) - cum((EPOCH - timedelta(days=5)).timestamp()))) < 1.0

    # karta (podsumowanie)
    summ = fuel._summary()
    cc = summ["counter_calibration"]
    hop = summ["hopper"]
    print("karta: collecting=%s | tydzień: %s | zasobnik: %s | notatka: %s" % (
        cc["collecting"], (summ["last7"] or {}).get("source"), hop["method"], hop["counter_note"]))
    if full_new == 0:
        assert cc["collecting"] and cc["message"].startswith(fuel.COUNTER_COLLECTING_MESSAGE)
        assert hop["counter_note"] == cc["message"] and hop["counter_provisional"] is True
        e_ts, now_ts, r_ts = EPOCH.timestamp(), datetime.now(TZ).timestamp(), refills[-1].timestamp()
        want = (cum(e_ts) - cum(r_ts)) + (cum(now_ts) - cum(e_ts)) * PROV / KPP_NEW   # przed epoką stary współczynnik, po niej tymczasowy
        if r_ts >= e_ts:
            want = (cum(now_ts) - cum(r_ts)) * PROV / KPP_NEW
        assert abs(hop["used_kg_counter"] - want) < 1.5, (hop["used_kg_counter"], want)
        assert "tymczasowy" in hop["method"] and "szacunek" in hop["method"] and "2,28" in hop["method"], hop["method"]
        assert (summ["last7"] or {}).get("source") == "licznik podajnika (szacunek)", summ["last7"]
    else:
        assert not cc["collecting"] and hop["counter_note"] is None and hop["counter_provisional"] is False
        assert hop["used_kg_counter"] is not None and (summ["last7"] or {}).get("source") == "licznik podajnika"
        # zużycie od ostatniej dosypki z licznika ≈ prawda
        want = cum(datetime.now(TZ).timestamp()) - cum(refills[-1].timestamp())
        assert abs(hop["used_kg_counter"] - want) < 1.5, (hop["used_kg_counter"], want)

    # moc palnika: osobna kalibracja przed i po epoce
    st = fuel._stats({"days": ["40"]})
    bc = st["calibration"]
    print("moc palnika: stary x%s (skalibr.=%s)  nowy x%s (skalibr.=%s, tymczasowy=%s)" % (
        bc["old"]["factor"], bc["old"]["calibrated"], bc["new"]["factor"], bc["new"]["calibrated"], bc["new"].get("provisional", False)))
    assert bc["old"]["calibrated"] and abs(bc["old"]["factor"] - F_OLD) < 0.03, bc["old"]
    if full_new == 0:
        assert not bc["new"]["calibrated"] and bc["new"]["provisional"] and bc["new"]["factor"] == fuel.BURNER_FACTOR_AFTER_EPOCH == 0.94
        assert bc["provisional"] and bc["factor"] == 0.94 and bc["calibrated"] is False
    else:
        assert bc["new"]["calibrated"] and abs(bc["new"]["factor"] - F_NEW) < 0.03 and not bc["new"].get("provisional"), bc["new"]
        assert abs(bc["factor"] - F_NEW) < 0.03 and not bc["provisional"]
    errs_b = {"old": [], "new": []}
    for r in st["daily"]:
        if r.get("est_kg") is None or r.get("samples", 0) < 1000:
            continue
        d = date.fromisoformat(r["day"])
        regime = fuel._counter_regime(d)
        if regime == "epoch":
            continue
        if regime == "new" and full_new == 0:
            assert r["est_provisional"] is True
            continue
        errs_b[regime].append(abs(r["est_kg"] - truth(d)[3]))
    assert errs_b["old"] and max(errs_b["old"]) < 0.7, errs_b["old"][:5]
    if full_new:
        assert errs_b["new"] and max(errs_b["new"]) < 0.7, errs_b["new"][:5]
    print("kg z mocy palnika dobowo: błąd maks. przed epoką %.2f kg%s" % (max(errs_b["old"]), (", po epoce %.2f kg" % max(errs_b["new"])) if errs_b["new"] else ""))

    # okresy: reżim licznika
    st = fuel._stats({"days": ["5"]})
    regimes = [p["counter_regime"] for p in st["periods"]]
    print("okresy:", regimes)
    assert regimes.count("epoch") == (1 if len(refills) > len(PRE) else 0)
print("\nEPOCH OK")
