"""Model dobowy z bezwładnością budynku: kandydat t_lag = 0,6 × t(doba) + 0,4 × t(doba − 1).

Uruchomienie z katalogu repo: python3 tests/test_daily_lag.py

Sprawdza:
- kandydat t_lag zostaje tylko wtedy, gdy poprawia błąd LOO o co najmniej 3 % (dane z bezwładnością:
  zostaje; dane bez bezwładności: odpada; tuż poniżej i tuż powyżej progu 3 %),
- t_lag łączy się ze słońcem, a stopniodni z t_lag liczone są przy bazie 15,5 °C,
- wiersze dobowe (_daily_rows) mają t_lag i hdd_lag, także dla pierwszej doby (poprzednia doba dociągana),
- prognoza (_rate_for, _days_until) korzysta z temperatury z wczoraj, również dla pierwszej doby prognozy.
"""
import math
import os
import random
import sys

sys.dont_write_bytecode = True
R = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
os.environ.update(THINGSPEAK_CHANNEL_IDS="1,2", THINGSPEAK_FUEL_CHANNEL_ID="3", FUEL_LAT="52.2",
                  FUEL_LON="21.0", FUEL_API_TOKEN="t", HDD_BASE="15.5")
os.environ.pop("FUEL_HDD_BASE_FIXED", None)
sys.path.insert(0, R)
import fuel
from datetime import date, datetime, timedelta, timezone

TZ = fuel.TZ
BASE = 15.5
assert fuel._active_base() == BASE and fuel.LAG_WEIGHT_TODAY == 0.6 and fuel.LAG_WEIGHT_PREVIOUS == 0.4
assert abs(fuel._lagged_temperature(10.0, 4.0) - 7.6) < 1e-12
assert fuel._lagged_temperature(None, 4.0) is None and fuel._lagged_temperature(10.0, None) is None
print("t_lag = 0,6 × dziś + 0,4 × wczoraj: OK")

# ---------------------------------------------------------------- syntetyczne wiersze dobowe (bez sieci)
N = 70
rng = random.Random(7)
temps = [4 + 4 * math.sin(i * 0.9) + 3 * math.sin(i * 0.37 + 1) for i in range(-1, N)]   # tu temperatura zmienia się z doby na dobę
solar = [3 + 5 * (1 + math.sin(i / 4.0)) / 2 for i in range(N)]
wind = [8 + 30 * rng.random() for _ in range(N)]
noise = [rng.gauss(0, 0.3) for _ in range(N)]
start_day = date(2026, 9, 1)


def rows_for(kg_of):
    """kg_of(i, t, t_lag, hdd, hdd_lag, solar, wind) -> kg doby i."""
    out = []
    for i in range(N):
        t, prev = temps[i + 1], temps[i]
        t_lag = fuel._lagged_temperature(t, prev)
        hdd, hdd_lag = max(0.0, BASE - t), max(0.0, BASE - t_lag)
        out.append({"day": (start_day + timedelta(days=i)).isoformat(), "t_mean": round(t, 2), "hdd": round(hdd, 2),
                    "t_lag": round(t_lag, 2), "hdd_lag": round(hdd_lag, 2), "solar_mj": round(solar[i], 2),
                    "wind_max_kmh": round(wind[i], 1), "kg_counter": round(kg_of(i, t, t_lag, hdd, hdd_lag, solar[i], wind[i]), 2),
                    "estimate": False})
    return out


def fit(rows):
    fuel._daily_model_cache.update(key=None, data=None)
    return fuel._fit_daily_model(rows)


def loo(model, terms):
    return next(c["rmse_loo_kg_per_day"] for c in model["candidates"] if c["terms"] == terms)


# 1. dane z bezwładnością: zużycie zależy od t_lag
m = fit(rows_for(lambda i, t, tl, h, hl, s, w: 5 + 3.4 * hl + noise[i]))
print("1. bezwładność:", m["terms"], "kg/stopień %.2f" % m["kg_per_hdd"], "LOO %.2f vs sama temperatura %.2f" % (m["rmse_loo_kg_per_day"], m["rmse_loo_temperature_only"]))
assert m["status"] == "aktywny" and m["terms"] == ["hdd_lag"], m["terms"]
assert m["temperature_term"] == "t_lag" and m["lag_weights"] == [0.6, 0.4] and abs(m["kg_per_hdd"] - 3.4) < 0.15
assert m["rmse_loo_kg_per_day"] < m["rmse_loo_temperature_only"] * (1 - fuel.DAILY_MODEL_MIN_GAIN)
assert any(c["terms"] == ["hdd"] for c in m["candidates"]) and any(c["terms"] == ["hdd_lag"] for c in m["candidates"])   # kandydat ze słońcem o złym znaku odpada

# 2. dane bez bezwładności: zużycie zależy od temperatury doby, t_lag odpada
m = fit(rows_for(lambda i, t, tl, h, hl, s, w: 5 + 3.4 * h + noise[i]))
gain = 1 - loo(m, ["hdd_lag"]) / loo(m, ["hdd"])
print("2. bez bezwładności:", m["terms"], "t_lag gorszy od temperatury doby o %.0f%% (LOO)" % (-gain * 100))
assert m["terms"] == ["hdd"] and m["lag_weights"] is None and m["temperature_term"] == "t_mean" and gain < fuel.DAILY_MODEL_MIN_GAIN

# 3. t_lag z innymi czynnikami: bezwładność i słońce
m = fit(rows_for(lambda i, t, tl, h, hl, s, w: 5 + 3.4 * hl - 0.5 * s + noise[i]))
print("3. bezwładność + słońce:", m["terms"], "słońce %.2f kg/MJ" % m["kg_per_solar_mj"])
assert m["terms"] == ["hdd_lag", "solar"] and abs(m["kg_per_solar_mj"] + 0.5) < 0.1 and abs(m["kg_per_hdd"] - 3.4) < 0.15

# 4. próg 3 %: kg to mieszanka temperatury doby i t_lag (a = udział bezwładności); t_lag zostaje wtedy i tylko wtedy,
#    gdy poprawia błąd LOO o co najmniej 3 %
def improvement(a):
    m = fit(rows_for(lambda i, t, tl, h, hl, s, w: 5 + 3.4 * ((1 - a) * h + a * hl) + noise[i]))
    return 1 - loo(m, ["hdd_lag"]) / loo(m, ["hdd"]), m


def find(target):
    lo, hi = 0.0, 1.0
    assert improvement(lo)[0] < target < improvement(hi)[0]
    for _ in range(40):
        mid = (lo + hi) / 2
        if improvement(mid)[0] < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


a_low, a_high = find(0.02), find(0.045)
gain_low, m_low = improvement(a_low)
gain_high, m_high = improvement(a_high)
print("4. próg 3%%: mieszanka %.3f daje poprawę LOO o %.1f%% -> %s; mieszanka %.3f daje %.1f%% -> %s" % (
    a_low, gain_low * 100, m_low["terms"], a_high, gain_high * 100, m_high["terms"]))
assert 0 < gain_low < fuel.DAILY_MODEL_MIN_GAIN and m_low["terms"] == ["hdd"], (gain_low, m_low["terms"])   # poprawia, ale za mało
assert gain_high >= fuel.DAILY_MODEL_MIN_GAIN and m_high["terms"] == ["hdd_lag"], (gain_high, m_high["terms"])
for a in (0.0, 0.25, 0.5, 0.75, 1.0):   # zgodność z regułą na całej skali
    g, mm = improvement(a)
    assert (mm["terms"] == ["hdd_lag"]) == (g >= fuel.DAILY_MODEL_MIN_GAIN), (a, g, mm["terms"])
print("   reguła 3 %% zgodna z wyborem dla a = 0 ... 1: OK")

# ---------------------------------------------------------------- wiersze dobowe z atrapy ThingSpeak i Open-Meteo
today = datetime.now(TZ).date()
D0 = today - timedelta(days=80)
world = {"inertia": True}


def t_of(d):
    i = (d - D0).days
    return 4 + 4 * math.sin(i * 0.9) + 3 * math.sin(i * 0.37 + 1)


def solar_of(d):
    return 3 + 4 * (1 + math.sin((d - D0).days / 5.0)) / 2


def wind_of(d):
    return 10 + 15 * (1 + math.sin((d - D0).days * 1.3)) / 2


def kg_of_day(d):
    t = t_of(d)
    effective = fuel._lagged_temperature(t, t_of(d - timedelta(days=1))) if world["inertia"] else t
    return 5 + 3.4 * max(0.0, BASE - effective)


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
        return {"daily": {"time": [d.isoformat() for d in days], "temperature_2m_mean": [round(t_of(d), 3) for d in days],
                          "shortwave_radiation_sum": [round(solar_of(d), 3) for d in days],
                          "wind_speed_10m_max": [round(wind_of(d), 2) for d in days]}}
    if "/channels/3/feeds.json" in url or "/channels/2/fields/8.json" in url or "/channels/2/feeds.json" in url:
        return {"feeds": []}
    if "/channels/1/feeds.json" in url:
        if params.get("results") == 1:
            return {"channel": {"created_at": iso(datetime.combine(D0, datetime.min.time(), tzinfo=TZ).timestamp())}, "feeds": []}
        s, e = parse(params["start"]), parse(params["end"])
        t = t_of(datetime.fromtimestamp(s + 3600, TZ).date())
        now = datetime.now(timezone.utc).timestamp()
        return {"feeds": [{"created_at": iso(x), "field4": str(round(t, 3)), "field8": "6.0"} for x in range(int(s), int(min(e, now)), 60)]}
    raise AssertionError("nieoczekiwane URL " + url)


fuel._request_json = fake
fuel._counter_day_diagnostic = lambda day, counter: (round(kg_of_day(day), 2), {"day": day.isoformat(), "reason": None})   # licznik podajnika zastąpiony prawdziwym zużyciem doby

rows, _counter = fuel._daily_rows(70)
assert len(rows) >= 60 and rows[0]["t_lag"] is not None
for prev, cur in zip(rows, rows[1:]):
    assert abs(cur["t_lag"] - (0.6 * cur["t_mean"] + 0.4 * prev["t_mean"])) < 0.012, (prev, cur)
    assert abs(cur["hdd_lag"] - max(0.0, BASE - cur["t_lag"])) < 0.012
first = rows[0]
want_first = 0.6 * t_of(date.fromisoformat(first["day"])) + 0.4 * t_of(date.fromisoformat(first["day"]) - timedelta(days=1))
assert abs(first["t_lag"] - want_first) < 0.012, (first, want_first)   # pierwsza doba też ma t_lag (poprzednia dociągnięta)
print("\nwiersze dobowe: %d dób, t_lag = 0,6 × dziś + 0,4 × wczoraj, także dla pierwszej doby: OK" % len(rows))

daily = fit(rows)
print("model z danych z bezwładnością (atrapa):", daily["terms"], "kg/stopień %.2f, stała %.2f" % (daily["kg_per_hdd"], daily["base_kg_per_day"]))
assert daily["terms"] == ["hdd_lag"] and abs(daily["kg_per_hdd"] - 3.4) < 0.05 and abs(daily["base_kg_per_day"] - 5.0) < 0.1
world["inertia"] = False
rows_flat, _ = fuel._daily_rows(70)
flat = fit(rows_flat)
print("model z danych bez bezwładności (atrapa):", flat["terms"])
assert flat["terms"] == ["hdd"]
world["inertia"] = True

# ---------------------------------------------------------------- prognoza z wczorajszą temperaturą
house = {"base_kg_per_day": 5.0, "kg_per_hdd": 3.4, "balance_temp": BASE}
day1 = today
t1, t2, t3, yesterday = 2.0, 9.0, 4.0, 12.0
w1, w2 = fuel._weather(day1), fuel._weather(day1 + timedelta(days=1))


def expect(model, hdd, weather):
    kg = model["base_kg_per_day"] + model["kg_per_hdd"] * hdd
    if model.get("kg_per_solar_mj") is not None:
        kg += model["kg_per_solar_mj"] * weather["solar_mj"]
    if model.get("kg_per_wind_hdd") is not None:
        kg += model["kg_per_wind_hdd"] * weather["wind_max_kmh"] * hdd / 100
    return max(kg, 0.0)


r_known = fuel._rate_for(house, daily, day1, t1, yesterday)
r_none = fuel._rate_for(house, daily, day1, t1, None)
hdd_known = max(0.0, BASE - (0.6 * t1 + 0.4 * yesterday))
hdd_none = max(0.0, BASE - t1)   # bez wczoraj temperatura efektywna to po prostu dzisiejsza
assert abs(r_known - expect(daily, hdd_known, w1)) < 1e-9 and abs(r_none - expect(daily, hdd_none, w1)) < 1e-9
assert abs(r_known - r_none) > 1.0   # ciepła wczorajsza doba obniża zużycie dzisiejszej (0,4 × (12 − 2) = 4 K różnicy)
print("_rate_for: z wczorajszą temperaturą %.1f kg, bez %.1f kg: OK" % (r_known, r_none))
flat_rate = fuel._rate_for(house, flat, day1, t1, yesterday)   # model bez bezwładności ignoruje wczoraj
assert abs(flat_rate - fuel._predict_kg_per_day(house, t1)) < 1e-9

now = datetime(today.year, today.month, today.day, 8, 0, tzinfo=TZ)
forecast = [(day1, t1), (day1 + timedelta(days=1), t2), (day1 + timedelta(days=2), t3)]
frac = (fuel._day_bounds(day1)[1] - now).total_seconds() / 86400
rate1 = expect(daily, max(0.0, BASE - (0.6 * t1 + 0.4 * yesterday)), w1)
rate2 = expect(daily, max(0.0, BASE - (0.6 * t2 + 0.4 * t1)), w2)   # wczoraj drugiej doby to prognoza pierwszej
need = rate1 * frac + rate2 * 0.5
got = fuel._days_until(need, house, forecast, now, daily, {day1 - timedelta(days=1): yesterday})
want = frac + 0.5
print("_days_until: potrzeba %.1f kg -> %.2f doby (oczekiwane %.2f)" % (need, got, want))
assert abs(got - round(want, 1)) < 0.051, (got, want)
no_yesterday = fuel._days_until(need, house, forecast, now, daily, None)
assert no_yesterday != got   # bez wczorajszej temperatury wynik jest inny, więc _days_until z niej korzysta
print("\nLAG OK")
