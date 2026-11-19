"""Pure mathematical models used by the ThermoFlow fuel engine.

This module has no network, clock, environment, cache or filesystem dependencies.
The legacy fuel.py facade re-exports these names during the compatibility period.
"""
from __future__ import annotations

def _solve(a: list, b: list):
    """Rozwiązanie układu a·x = b (eliminacja Gaussa z wyborem elementu)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]

def _ols_loo(rows: list, y: list):
    """Regresja liniowa z błędem prognozy liczonym metodą pomijania po jednej dobie (LOO)."""
    k = len(rows[0])
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * yi for r, yi in zip(rows, y)) for i in range(k)]
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    inv_cols = [_solve(xtx, [1.0 if i == j else 0.0 for i in range(k)]) for j in range(k)]
    if any(col is None for col in inv_cols):
        return None
    inv = [[inv_cols[j][i] for j in range(k)] for i in range(k)]
    loo, fit = [], []
    for r, yi in zip(rows, y):
        pred = sum(b * x for b, x in zip(beta, r))
        h = sum(r[i] * sum(inv[i][j] * r[j] for j in range(k)) for i in range(k))
        fit.append(yi - pred)
        loo.append((yi - pred) / (1 - h) if h < 0.999 else yi - pred)
    return {"beta": beta,
            "rmse": (sum(e * e for e in fit) / len(fit)) ** 0.5,
            "rmse_loo": (sum(e * e for e in loo) / len(loo)) ** 0.5}

def _lagged_temperature(t, prev_t):
    """Temperatura efektywna z bezwładnością budynku: 0,6 × dziś + 0,4 × wczoraj (None, gdy brak którejś doby)."""
    if t is None or prev_t is None:
        return None
    return LAG_WEIGHT_TODAY * t + LAG_WEIGHT_PREVIOUS * prev_t

def _fit_at(rows: list, base: float):
    """Regresja kg/dobę = stała + kg_na_stopień × stopniodni/dobę przy zadanej temperaturze bazowej."""
    w = [days for days, _kg, _t in rows]
    x = [_hdd_from(temps, base) / days for days, _kg, temps in rows]
    y = [kg / days for days, kg, _t in rows]
    sw = sum(w)
    mx = sum(wi * xi for wi, xi in zip(w, x)) / sw
    my = sum(wi * yi for wi, yi in zip(w, y)) / sw
    sxx = sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, x))
    if sxx <= 1e-9:
        return None
    slope = sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, x, y)) / sxx
    intercept = my - slope * mx
    if intercept < 0:   # stała część nie może być ujemna: prosta przez zero
        intercept = 0.0
        slope = sum(wi * xi * yi for wi, xi, yi in zip(w, x, y)) / sum(wi * xi * xi for wi, xi in zip(w, x))
    if slope <= 0:
        return None
    sse = sum(wi * (yi - intercept - slope * xi) ** 2 for wi, xi, yi in zip(w, x, y))
    sst = sum(wi * (yi - my) ** 2 for wi, yi in zip(w, y))
    return {
        "balance_temp": base,
        "base_kg_per_day": round(intercept, 2),
        "kg_per_hdd": round(slope, 2),
        "r2": round(1 - sse / sst, 3) if sst > 0 else None,
        "rms_kg_per_day": round((sse / sw) ** 0.5, 1),
        "periods_used": len(rows),
        "_sse": sse,
    }

def _predict_kg_per_day(model: dict, t_mean: float) -> float:
    return model["base_kg_per_day"] + model["kg_per_hdd"] * max(0.0, model["balance_temp"] - t_mean)

def _predict_daily(model: dict, hdd: float, solar, wind):
    kg = model["base_kg_per_day"] + model["kg_per_hdd"] * hdd
    if model.get("kg_per_solar_mj") is not None and solar is not None:
        kg += model["kg_per_solar_mj"] * solar
    if model.get("kg_per_wind_hdd") is not None and wind is not None:
        kg += model["kg_per_wind_hdd"] * wind * hdd / 100
    return max(kg, 0.0)
