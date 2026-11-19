"""Daily fuel-model candidate selection, isolated from the legacy facade.

Policy constants, model fitting and cache storage are injected so this module
can be tested deterministically without ThingSpeak or Open-Meteo.
"""
from __future__ import annotations

def fit_daily_model(rows: list, *, cache: dict, active_base, min_days: int,
                    lag_weight_today: float, lag_weight_previous: float,
                    min_gain: float, ols_loo):
    """Model dobowy: sprawdza, czy bezwładność, słońce i wiatr poprawiają prognozę ponad samą temperaturę.

    Kandydaci: stopniodni z temperatury doby (hdd) albo z temperatury z bezwładnością
    t_lag = 0,6 × dziś + 0,4 × wczoraj (hdd_lag), każde samo albo ze słońcem i wiatrem.
    Każdy dodatkowy element zostaje tylko wtedy, gdy zmniejsza błąd prognozy dla dób,
    których model nie widział (LOO), o co najmniej min_gain i ma sensowny znak
    (słońce zmniejsza zużycie, wiatr przy mrozie i niższa temperatura je zwiększają).
    """
    usable = [r for r in rows if r["kg_counter"] is not None and r["hdd"] is not None
              and not r.get("estimate")   # doby ze współczynnikiem tymczasowym nie uczą modelu
              and r.get("hdd_lag") is not None   # wszyscy kandydaci uczą się na tych samych dobach
              and r["solar_mj"] is not None and r["wind_max_kmh"] is not None]
    key = (len(usable), usable[-1]["day"] if usable else None, active_base(),
           round(sum(r["kg_counter"] for r in usable), 1))
    if cache["key"] == key:
        return cache["data"]
    status = {"status": "zbiera dane", "days": len(usable), "days_needed": min_days}
    if len(usable) < min_days:
        _daily_model_cache.update(key=key, data=status)
        return status

    y = [r["kg_counter"] for r in usable]
    features = {
        "hdd": lambda r: r["hdd"],
        "hdd_lag": lambda r: r["hdd_lag"],
        "solar": lambda r: r["solar_mj"],
        "wind_hdd": lambda r: r["wind_max_kmh"] * r["hdd"] / 100,
        "wind_hdd_lag": lambda r: r["wind_max_kmh"] * r["hdd_lag"] / 100,
    }
    expected_sign = {"solar": -1, "wind_hdd": 1, "wind_hdd_lag": 1, "hdd_lag": 1}
    candidates = [["hdd"], ["hdd", "solar"], ["hdd", "wind_hdd"], ["hdd", "solar", "wind_hdd"],
                  ["hdd_lag"], ["hdd_lag", "solar"], ["hdd_lag", "wind_hdd_lag"], ["hdd_lag", "solar", "wind_hdd_lag"]]
    results = []
    for names in candidates:
        rows_x = [[1.0] + [features[n](r) for n in names] for r in usable]
        fit = ols_loo(rows_x, y)
        if not fit:
            continue
        coefs = dict(zip(["base"] + names, fit["beta"]))
        if any(n in expected_sign and coefs[n] * expected_sign[n] < 0 for n in names):
            continue   # znak niezgodny z fizyką: czynnik odrzucony
        results.append({"terms": names, "coefs": coefs, **fit})
    if not results:
        _daily_model_cache.update(key=key, data=status)
        return status
    baseline = next((r for r in results if r["terms"] == ["hdd"]), results[0])
    best = baseline
    for r in results:
        if r is not baseline and r["rmse_loo"] < best["rmse_loo"] * (1 - min_gain):
            best = r
    c = best["coefs"]
    lag = "hdd_lag" in best["terms"]
    wind_key = "wind_hdd_lag" if lag else "wind_hdd"
    data = {
        "status": "aktywny",
        "days": len(usable),
        "balance_temp": active_base(),
        "terms": best["terms"],
        "temperature_term": "t_lag" if lag else "t_mean",
        "lag_weights": [lag_weight_today, lag_weight_previous] if lag else None,
        "base_kg_per_day": round(c["base"], 2),
        "kg_per_hdd": round(c["hdd_lag" if lag else "hdd"], 2),
        "kg_per_solar_mj": round(c["solar"], 3) if "solar" in c else None,
        "kg_per_wind_hdd": round(c[wind_key], 3) if wind_key in c else None,
        "rmse_kg_per_day": round(best["rmse"], 1),
        "rmse_loo_kg_per_day": round(best["rmse_loo"], 1),
        "rmse_loo_temperature_only": round(baseline["rmse_loo"], 1),
        "candidates": [{"terms": r["terms"], "rmse_loo_kg_per_day": round(r["rmse_loo"], 2)} for r in results],
    }
    _daily_model_cache.update(key=key, data=data)
    return data


