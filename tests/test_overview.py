"""Regression checks for the read-only ThermoFlow Sprint 2 Overview."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
OVERVIEW = (ROOT / "assets/js/overview.js").read_text(encoding="utf-8")
LIVE = (ROOT / "assets/js/live.js").read_text(encoding="utf-8")
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
assert "valve.return" in OVERVIEW
assert "55 °C ±1 °C" in HTML
assert "STB pozostaje bez odczytu" in HTML
assert "POST" not in OVERVIEW and "DELETE" not in OVERVIEW
assert "setInterval" not in NAV, "Navigation must not overwrite live Overview values"
assert "reference-model" not in OVERVIEW
print(f"Overview regression: {len(required_ids)} required elements, API event wiring, read-only behavior: OK")
