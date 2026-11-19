"""Regression checks for the read-only ThermoFlow Sprint 2 Overview."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
OVERVIEW = (ROOT / "assets/js/overview.js").read_text(encoding="utf-8")
LIVE = (ROOT / "assets/js/live.js").read_text(encoding="utf-8")
API = (ROOT / "api.py").read_text(encoding="utf-8")
FUEL = (ROOT / "assets/js/fuel.js").read_text(encoding="utf-8")
NAV = (ROOT / "assets/js/navigation.js").read_text(encoding="utf-8")

required_ids = {
    "overviewConnection", "overviewCo", "overviewReturn", "overviewOutdoor",
    "overviewFlue", "overviewFuel", "overviewBoilerState", "overviewUpdated",
    "overviewAlerts", "overviewPumps", "overviewFuelModels",
}
for element_id in required_ids:
    assert f'id="{element_id}"' in HTML, f"Missing Overview element: {element_id}"

assert '/assets/js/overview.js' in HTML
assert "thermoflow:status" in LIVE and "thermoflow:status" in OVERVIEW
assert "thermoflow:fuel" in FUEL and "thermoflow:fuel" in OVERVIEW
assert "/api/status" in LIVE
assert "/api/fuel/summary" in FUEL
assert "daily_model" in OVERVIEW and "days_needed" in OVERVIEW
assert "periods_used" in OVERVIEW and "kg_per_hdd" in OVERVIEW
assert "v.return" in OVERVIEW
assert "55 °C ±1 °C" in HTML
assert "STB pozostaje bez odczytu" in HTML
assert "POST" not in OVERVIEW and "DELETE" not in OVERVIEW
assert "setInterval" not in NAV, "Navigation must not overwrite live Overview values"
assert "reference-model" not in OVERVIEW
print(f"Overview regression: {len(required_ids)} required elements, API event wiring, read-only behavior: OK")


# Sprint 3 read-only diagnostics and truthful live indicator.
DIAGNOSTICS = (ROOT / "assets/js/diagnostics.js").read_text(encoding="utf-8")
MOTION = (ROOT / "assets/css/motion.css").read_text(encoding="utf-8")
assert 'id="diagnosticsHistoryState"' in HTML
assert 'id="diagnosticsSummary"' in HTML and 'id="diagnosticsEvents"' in HTML
assert '/assets/js/diagnostics.js' in HTML
assert '/api/history?hours=24&format=full' in DIAGNOSTICS
assert "coCycles" in DIAGNOSTICS and "burner" in DIAGNOSTICS and "cold" in DIAGNOSTICS
assert "POST" not in DIAGNOSTICS and "DELETE" not in DIAGNOSTICS
assert "age<=90" in LIVE and "classList.toggle('live',fresh)" in LIVE
assert ".badge.live::before" in MOTION and "emodul-live-pulse" in MOTION
assert "good live" in OVERVIEW
print("Sprint 3 diagnostics: history analysis, read-only event list, truthful eModul live indicator: OK")

# Sprint 4 deep diagnostics: full-resolution, read-only history analysis.
assert 'format=full' in DIAGNOSTICS
for series_id in (
    "thermoflow-co", "thermoflow-flue", "thermoflow-boiler-power",
    "thermoflow-valve4d-return", "thermoflow-valve4d-opening",
    "thermoflow-valve4d-effective-setpoint", "thermoflow-pump-floor",
    "thermoflow-pump-dhw", "thermoflow-pump-valve4d",
):
    assert series_id in DIAGNOSTICS, f"Missing diagnostics series: {series_id}"
for threshold in ("lowThreshold!=null&&v<lowThreshold", "targetThreshold!=null&&v>=targetThreshold"):
    assert threshold in DIAGNOSTICS, f"Missing diagnostics threshold: {threshold}"
assert "pumpImpact" in DIAGNOSTICS and "średnia zmiana powrotu" in DIAGNOSTICS
assert "function quality(data,maxGapSeconds)" in DIAGNOSTICS and "maxGapSeconds" in DIAGNOSTICS
assert "function analysisSettings(payload)" in DIAGNOSTICS
assert "ThermoFlowApi.get('/api/diagnostics')" in DIAGNOSTICS
assert "v<=minimumOpening" in DIAGNOSTICS
assert "settings?.co_low_threshold" in DIAGNOSTICS
assert "settings?.co_recovery_target" in DIAGNOSTICS
assert "settings?.return_protection_min" in DIAGNOSTICS
assert "settings?.max_sample_gap_seconds" in DIAGNOSTICS
assert "settings?.pump_correlation_window_seconds" in DIAGNOSTICS
assert "settings?.return_protection_tolerance" in DIAGNOSTICS
assert "settings?.boiler_power_active_min_kw" in DIAGNOSTICS
assert "settings?.valve_change_min_delta" in DIAGNOSTICS
assert '"analysis_config": analysis_config' in API
assert 'configured_menu_value(2203)' in API
assert 'configured_menu_value(2060)' in API and 'configured_menu_value(2062)' in API
assert "v<64" not in DIAGNOSTICS and "v>=70" not in DIAGNOSTICS and "v<55" not in DIAGNOSTICS
assert "MAX_GAP=300" not in DIAGNOSTICS
assert "próg z konfiguracji eModul" in DIAGNOSTICS
assert "v<=5" not in DIAGNOSTICS and "otwarciu ≤5%" not in DIAGNOSTICS
assert "interrupted:true" in DIAGNOSTICS and "ANALIZA CZĘŚCIOWA" in DIAGNOSTICS
assert "tylko kompletne cykle bez luk" in DIAGNOSTICS
assert "55°C ±1°C" in HTML
assert "setInterval(load,60000)" in DIAGNOSTICS
assert "POST" not in DIAGNOSTICS and "DELETE" not in DIAGNOSTICS
print("Sprint 4 diagnostics: boiler cycles, return protection, valve and pump correlation, read-only: OK")
