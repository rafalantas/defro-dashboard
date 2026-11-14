"""Kalibracja licznika przed i po zmianie podajnika (FUEL_COUNTER_EPOCH), z atrapą ThingSpeak i Open-Meteo.

Uruchomienie z katalogu repo: python3 tests/test_counter_epoch.py

Licznik zapasu w atrapie jest zaokrąglany do 1 % (tak jak w eModul), więc błędy dobowe są rzędu
kilku kg. Sprawdza:
- scalanie wpisów dosypek bliższych niż 30 minut (6 kg i 84 kg w odstępie 27 s to jedna dosypka 90 kg),
- tymczasowy współczynnik po epoce 3,4 kg/% (szacunek, ±10 %), dopóki nie ma żadnego pomiaru,
- oszacowanie współczynnika po epoce z okresu obejmującego epokę (część przed epoką wg starego,
  reszta kg przypisana części po epoce), używane zamiast wartości tymczasowej,
- kalibrację z pełnych okresów po epoce,
- osobną kalibrację mocy palnika przed i po epoce.
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
fuel.TS_WRITE_KEY_FUEL = "w"
KPP_OLD, KPP_NEW = 3.2, 2.4   # prawdziwy kg na 1 % licznika przed i po epoce
F_OLD, F_NEW = 1.7, 0.80      # współczynnik mocy palnika przed i po epoce (po epoce inny niż oczekiwane 0,94)
QUANT = 4.5                   # tolerancja dobowa na zaokrąglenie licznika do 1 % (ok. 1 pp * 3,2-3,4 kg)


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


refill_entries = []   # [(czas, kg)] ustawiane per scenariusz
written = []


def entries_for(times):
    out = []
    for i, t in enumerate(times):
        kg = cum(t.timestamp()) - cum(times[i - 1].timestamp()) if i else 240.0
        out.append((t, round(kg, 1)))
    return out


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
    if "/update.json" in url:
        written.append(form)
        return {"entry_id": 9000 + len(written)}
    if "/channels/3/feeds.json" in url:
        feeds = [{"entry_id": i + 1, "created_at": iso(t.timestamp()), "field1": str(kg), "field2": None, "field3": None}
                 for i, (t, kg) in enumerate(refill_entries)]
        feeds += [{"entry_id": 500 + i, "created_at": iso(datetime.now(timezone.utc).timestamp() - 60 + i),
                   "field1": None, "field2": None, "field3": str(f["field3"])} for i, f in enumerate(written) if "field3" in f]
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
        # licznik zapasu zaokrąglany do pełnego procenta
        return {"feeds": [{"created_at": iso(x), "field8": str(round(100 - pct(x) % 70))}
                          for x in range(int(s), int(min(e, now)), 600)]}
    raise AssertionError("nieoczekiwane URL " + url)


fuel._request_json = fake


def reset(entries):
    global refill_entries
    refill_entries = entries
    del written[:]
    for cache in (fuel._refills_cache, fuel._summary_cache, fuel._offset_cache, fuel._forecast_cache):
        cache.update(at=0.0, data=None)
    fuel._model_cache.update(key=None, data=None)
    fuel._daily_model_cache.update(key=None, data=None)
    fuel._summary_state.update(thread=None, error=None)


# ---------------------------------------------------------------- scalanie wpisów dosypek (bez sieci)
t0 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=TZ)
rows_in = [{"id": 1, "ts": (t0 - timedelta(days=3)).isoformat(), "kg": 100.0, "note": None},
           {"id": 2, "ts": t0.isoformat(), "kg": 6.0, "note": "pierwsza"},
           {"id": 3, "ts": (t0 + timedelta(seconds=27)).isoformat(), "kg": 84.0, "note": None},
           {"id": 4, "ts": (t0 + timedelta(minutes=27)).isoformat(), "kg": 10.0, "note": None},   # 26 min 33 s po poprzednim: scala się
           {"id": 5, "ts": (t0 + timedelta(minutes=27 + 31)).isoformat(), "kg": 50.0, "note": None}]   # 31 min po poprzednim: osobna
merged = fuel._merge_close_refills(rows_in)
assert [m["kg"] for m in merged] == [100.0, 100.0, 50.0], merged
assert merged[1]["ts"] == (t0 + timedelta(minutes=27)).isoformat() and merged[1]["id"] == 4 and merged[1]["ids"] == [2, 3, 4]
assert merged[1]["note"] == "pierwsza"
two = fuel._merge_close_refills([{"id": 7, "ts": t0.isoformat(), "kg": 6.0, "note": None},
                                 {"id": 8, "ts": (t0 + timedelta(seconds=27)).isoformat(), "kg": 84.0, "note": None}])
assert len(two) == 1 and two[0]["kg"] == 90.0 and two[0]["ts"] == (t0 + timedelta(seconds=27)).isoformat() and two[0]["id"] == 8, two
gap29 = fuel._merge_close_refills([{"id": 1, "ts": t0.isoformat(), "kg": 1.0, "note": None},
                                   {"id": 2, "ts": (t0 + timedelta(minutes=29, seconds=59)).isoformat(), "kg": 2.0, "note": None}])
gap30 = fuel._merge_close_refills([{"id": 1, "ts": t0.isoformat(), "kg": 1.0, "note": None},
                                   {"id": 2, "ts": (t0 + timedelta(minutes=30)).isoformat(), "kg": 2.0, "note": None}])
assert len(gap29) == 1 and len(gap30) == 2
print("scalanie wpisów (6 kg + 84 kg w odstępie 27 s = jedna dosypka 90 kg; 30 min to już osobne): OK")

# ---------------------------------------------------------------- parsowanie epoki
assert fuel._parse_epoch("") == datetime.fromisoformat("2026-11-13T18:27:00+01:00")
assert fuel._parse_epoch("2026-11-13T18:27:00") == datetime(2026, 11, 13, 18, 27, tzinfo=TZ)
assert fuel._parse_epoch("bzdura") == datetime.fromisoformat("2026-11-13T18:27:00+01:00")
assert fuel._parse_epoch("2026-12-01T08:00:00+00:00").utcoffset() == timedelta(0)
assert fuel.COUNTER_PROVISIONAL_KG_PER_PERCENT == 3.4
print("parsowanie epoki i wartość tymczasowa 3,4: OK")

PRE = [at(80 - 7 * k) for k in range(0, 9)]   # co 7 dni od D0 do today-24 (przed epoką: today-20 18:27)
assert PRE[-1] < EPOCH


def run(name, times, kind, split_last=False):
    """kind: provisional (brak dosypki po epoce) | span (okres obejmujący epokę) | full (pełny okres po epoce)."""
    print("\n== " + name)
    fuel._counter_cache.clear()
    entries = entries_for(times)
    if split_last:   # ostatnia dosypka dodana jako dwa wpisy: 6 kg i reszta 27 s później
        t_last, kg_last = entries[-1]
        entries[-1:] = [(t_last, 6.0), (t_last + timedelta(seconds=27), round(kg_last - 6.0, 1))]
    reset(entries)
    daily = fuel._daily({"days": ["70"]})
    cal = daily["counter_calibration"]
    old, eff = cal["old"], cal["new_effective"]
    print("stary %s | efektywny po epoce %s (%s) | collecting=%s" % (old["kg_per_percent"], eff["kg_per_percent"], eff["source"], cal["collecting"]))
    assert old["calibrated"] and abs(old["kg_per_percent"] - KPP_OLD) < 0.15, old   # licznik w pełnych procentach
    if kind == "provisional":
        assert eff["source"] == "wartość tymczasowa" and not eff["measured"] and eff["kg_per_percent"] == 3.4
        assert eff["uncertainty_percent"] == 10 and cal["span"] is None and not cal["new"]["calibrated"]
        assert cal["collecting"] and cal["calibrated"] is False and cal["kg_per_percent"] == 3.4
        assert cal["message"].endswith("tymczasowo 3,40 kg/% (szacunek, ±10%)"), cal["message"]
    elif kind == "span":
        span = cal["span"]
        assert span and not cal["new"]["calibrated"] and eff["source"] == "okres obejmujący epokę" and eff["measured"]
        assert eff["kg_per_percent"] == span["kg_per_percent"] != 3.4   # użyte zamiast tymczasowego
        assert abs(span["kg_per_percent"] / KPP_NEW - 1) < 0.10, span   # w granicach deklarowanej niepewności
        assert span["uncertainty_percent"] == 10
        assert abs(span["high"] / span["kg_per_percent"] - 1.10) < 1e-3 and abs(span["low"] / span["kg_per_percent"] - 0.90) < 1e-3
        last_kg = sum(kg for t, kg in entries if t >= EPOCH)
        assert abs(span["kg_before"] + span["kg_after"] - last_kg) < 0.2, (span, last_kg)   # kg rozdzielone między część przed i po epoce
        assert span["drop_before_percent"] > 0 and span["drop_after_percent"] >= fuel.MIN_SPAN_DROP
        assert cal["collecting"] and "okresu obejmującego" in cal["message"] and "±10%" in cal["message"], cal["message"]
    else:
        assert cal["new"]["calibrated"] and abs(cal["new"]["kg_per_percent"] / KPP_NEW - 1) < 0.04, cal["new"]
        assert eff["source"] == "okresy po epoce" and not cal["collecting"] and cal["calibrated"] and cal["message"] is None
    eff_kpp = eff["kg_per_percent"]

    # --- doby (licznik zaokrąglany do 1 %)
    rows = {r["day"]: r for r in daily["days"]}
    epoch_day = EPOCH.date().isoformat()
    errs = {"old": [], "new": []}
    for day, r in rows.items():
        d = date.fromisoformat(day)
        regime = r["counter_regime"]
        if regime == "epoch":
            assert day == epoch_day and r["kg_counter"] is None, r
        elif regime == "old":
            assert r["estimate"] is False
            if r["kg_counter"] is not None:
                errs["old"].append(r["kg_counter"] - truth(d)[3])
        else:
            assert r["kg_counter"] is not None and r["estimate"] is (kind != "full"), r
            want = truth(d)[3] * (eff_kpp / KPP_NEW)   # wynik wg współczynnika obowiązującego po epoce
            errs["new"].append(r["kg_counter"] - want)
    assert errs["old"] and max(abs(e) for e in errs["old"]) < QUANT, errs["old"][:5]
    assert errs["new"] and max(abs(e) for e in errs["new"]) < QUANT * 1.2, errs["new"][:5]
    assert abs(sum(errs["old"]) / len(errs["old"])) < 1.0   # zaokrąglenia się uśredniają
    print("doby przed epoką: %d (błąd maks. %.1f kg), po epoce: %d (błąd maks. %.1f kg, %s)" % (
        len(errs["old"]), max(abs(e) for e in errs["old"]), len(errs["new"]), max(abs(e) for e in errs["new"]),
        "skalibrowane" if kind == "full" else "szacunek"))
    dm = daily["daily_model"]
    exact = [r for r in daily["days"] if r["kg_counter"] is not None and not r["estimate"]]
    assert dm["days"] == len(exact), (dm["days"], len(exact))   # doby-szacunki nie uczą modelu
    print("model dobowy:", dm["status"], "dób:", dm["days"])

    # --- przedział przez epokę: stary współczynnik przed, nowy (lub szacunek) po
    a, b = EPOCH - timedelta(days=2), EPOCH + timedelta(days=2)
    kg, ok, _ = fuel._counter_kg(a, b, cal)
    want = (cum(EPOCH.timestamp()) - cum(a.timestamp())) + (cum(b.timestamp()) - cum(EPOCH.timestamp())) * eff_kpp / KPP_NEW
    assert ok and abs(kg - want) < 6.0, (kg, want)
    print("przedział przez epokę: %.1f kg (oczekiwane %.1f)" % (kg, want))

    # --- karta
    summ = fuel._summary()
    cc, hop, week = summ["counter_calibration"], summ["hopper"], summ["last7"] or {}
    print("karta: tydzień: %s | zasobnik: %s" % (week.get("source"), hop["method"]))
    if cal["collecting"]:
        assert cc["collecting"] and hop["counter_note"] == cc["message"] and hop["counter_provisional"] is True
        assert week.get("source") == "licznik podajnika (szacunek)", week
        assert "szacunek" in hop["method"] and "±10%" in hop["method"]
        assert ("z okresu obejmującego" in hop["method"]) if kind == "span" else ("tymczasowy" in hop["method"] and "3,40" in hop["method"]), hop["method"]
    else:
        assert hop["counter_provisional"] is False and hop["method"] == "licznik podajnika skalibrowany do dosypek", hop["method"]
        assert week.get("source") == "licznik podajnika", week
    if split_last:
        merged_refills = fuel._refills()
        assert len(merged_refills) == len(times), (len(merged_refills), len(times))   # 6 + 84 to jedna dosypka
        last = merged_refills[-1]
        total = entries_for(times)[-1][1]
        assert abs(last["kg"] - total) < 0.11 and len(last["ids"]) == 2, last
        assert last["ts"] == (times[-1] + timedelta(seconds=27)).isoformat()
        assert len(fuel._refills_raw()) == len(times) + 1
        assert abs(summ["last_refill"]["kg"] - total) < 0.11
        print("scalona dosypka: %.1f kg (6,0 + %.1f), czas ostatniego wpisu, ids=%s" % (last["kg"], total - 6.0, last["ids"]))
        status, payload = fuel.handle("GET", "/api/fuel/refills", {}, {}, b"")
        assert status == 200 and len(payload["refills"]) == len(times) and payload["refills"][-1]["kg"] == last["kg"]

    # --- moc palnika: osobna kalibracja przed i po epoce
    st = fuel._stats({"days": ["40"]})
    bc = st["calibration"]
    print("moc palnika: stary x%s  nowy x%s (skalibr.=%s, tymczasowy=%s)" % (
        bc["old"]["factor"], bc["new"]["factor"], bc["new"]["calibrated"], bc["new"].get("provisional", False)))
    assert bc["old"]["calibrated"] and abs(bc["old"]["factor"] - F_OLD) < 0.03, bc["old"]
    if kind != "full":
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
        start, end = fuel._day_bounds(d)
        if d >= today or start < EPOCH < end:   # dzisiejsza doba jest niepełna, doba zmiany podajnika ma dwa współczynniki
            continue
        if start >= EPOCH and kind != "full":
            assert r["est_provisional"] is True
            continue
        errs_b["old" if end <= EPOCH else "new"].append(abs(r["est_kg"] - truth(d)[3]))
    assert errs_b["old"] and max(errs_b["old"]) < 0.7, errs_b["old"][:5]   # moc palnika nie jest zaokrąglana
    if kind == "full":
        assert errs_b["new"] and max(errs_b["new"]) < 0.7, errs_b["new"][:5]
    if split_last:   # na końcu, bo anulowanie zmienia dane dosypek
        last = fuel._refills()[-1]
        voided = fuel._void_refill(str(last["ids"][0]))
        assert voided["voided"] == last["ids"][0] and written and written[-1]["field3"] == last["ids"][0]
        assert len(fuel._refills()[-1]["ids"]) == 1 and abs(fuel._refills()[-1]["kg"] - (last["kg"] - 6.0)) < 0.11   # anulowany wpis 6 kg nie liczy się
        try:
            fuel._void_refill("123456")
            raise SystemExit("anulowanie nieistniejącego wpisu powinno dać 404")
        except fuel.FuelError as e:
            assert e.status == 404
        print("anulowanie pojedynczego wpisu z scalonej dosypki: OK")


run("A: brak dosypki po epoce (tymczasowo 3,4 kg/%, szacunek ±10%)", PRE, "provisional")
run("B: jedna dosypka po epoce (współczynnik z okresu obejmującego epokę zamiast 3,4)", PRE + [at(17)], "span")
run("B2: ta sama dosypka dodana jako 6 kg + 84 kg w odstępie 27 s (po scaleniu to samo oszacowanie)", PRE + [at(17)], "span", split_last=True)
run("C: jeden pełny okres po epoce (nowy współczynnik z dosypek)", PRE + [at(17), at(10)], "full")
run("D: dwa pełne okresy po epoce, ostatnia dosypka jako 6 kg + 84 kg (27 s)", PRE + [at(17), at(10), at(3)], "full", split_last=True)
print("\nEPOCH OK")
