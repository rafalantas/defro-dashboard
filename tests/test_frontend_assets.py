"""Static checks for the split ThermoFlow frontend."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

BASELINE_IDS = ["buildVersion","clock","modeBadge","pauseBtn","arrHot","arrBlue","arrWarm","arrMix","floorMixedFlowPath","floorReturnFlowPath","floorHotFeedPath","commonReturnPath","floorReturnToCommonPath","radiatorLoop","dhwLoop","circLoop","beadFloorHotFeed","beadFloor","beadFloorReturn","beadFloorReturnToCommon","beadRadiator","beadDhw","beadCirc","sensorFlueSvg","boilerFlame","boilerStateSvg","boilerPowerSvg","boilerFuelBar","boilerFuelSvg","sensorFireSvg","sensorTemp","valve4Svg","radRotor","radTemp","radCurrent","radCorrection","radSet","valve3Svg","floorRotor","floorPumpState","valve3Temp","floorTemp","dhwRotor","dhwTemp","circRotor","sensorOutdoorSvg","floorReturnJunction","returnTempSvg","mBoiler","mFloor","floorMetricHint","mRad","mDhw","dhwMetricHint","tileCo","tileCoMeta","tileDhw","tileDhwMeta","tileFlue","tileOutdoor","tileBoilerPower","tileBoilerPowerMeta","tileFire","tileFireMeta","tileFeeder","tileHeater","tilePumpFloor","tilePumpValve4d","tilePumpDhw","tilePumpCirc","tileFan","tileFanMeta","tileValve","tileValveMeta","tileFuel","tileFuelMeta","tileDisinfection","tileMode","tileControllerState","tileDhwState","tileDhwStateMeta","tileRoomState","tileRoomStateMeta","tileController","tileVersion","extraTempHeading","extraTempGrid","telemetryFooter","fuelCard","fuelTitle","fuelWeek","fuelWeekMeta","fuelHddLabel","fuelHdd","fuelHddMeta","fuelSince","fuelSinceMeta","fuelHopperTile","fuelHopper","fuelHopperMeta","fuelFooter","valve4Tooltip","sensorOutdoor","sensorReturn","sensorValve","sensorDhw","sensorFire","sensorCo","sensorFlue","sensorStb","sensorRoom","sensorSupply","statsTitle","statsDescription","statsPrev","statsDate","statsMonth","statsYear","statsNext","statsCsv","statsLegend","statsSeriesToggle","statsChart","statsTooltip","statsMessage","statsMeta","bottomNote"]

class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.assets = []
        self.inline_scripts = 0
        self.inline_styles = 0
        self.schematic = False
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in ("script", "link"):
            path = attrs.get("src") or attrs.get("href", "")
            if path.startswith("/assets/"):
                parsed = urlsplit(path)
                self.assets.append(parsed.path.lstrip("/"))
                assert parsed.query == "v=__APP_COMMIT__", f"Asset URL must be commit-versioned: {path}"
            if tag == "script" and "src" not in attrs:
                self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        if tag == "svg" and attrs.get("class") == "schematic" and attrs.get("viewbox") == "0 0 1380 600":
            self.schematic = True

audit = Audit()
audit.feed(HTML)
assert set(BASELINE_IDS).issubset(audit.ids), f"Missing baseline IDs: {sorted(set(BASELINE_IDS) - set(audit.ids))}"
assert len(audit.ids) == len(set(audit.ids)), "Duplicate HTML IDs"
assert not audit.inline_scripts, "JavaScript must stay in external modules"
assert not audit.inline_styles, "CSS must stay in external stylesheets"
assert audit.schematic, "Original inline installation SVG must remain present"
assert '<section class="card diagnostics-history-card" data-view="diagnostics" aria-labelledby="intelligenceTitle" hidden>' in HTML
assert "assets/js/intelligence-panel.js" in audit.assets
assert {"intelligenceLive", "intelligenceBenchmarks", "intelligenceRefresh", "intelligenceStatus",
        "shadowStatus", "diagnosticsHistoryTitle"}.issubset(audit.ids)
for asset in audit.assets:
    assert (ROOT / asset).is_file(), f"Missing frontend asset: {asset}"
print(f"Frontend assets: {len(audit.assets)} paths, {len(audit.ids)} stable IDs, inline SVG: OK")


def test_refill_history_does_not_render_notes():
    """Refill history shows timestamp and weight only; notes are not used by the operator."""
    source = (ROOT / "assets/js/fuel-panel.js").read_text(encoding="utf-8")
    render = source.split("function renderRefills(){", 1)[1].split("function renderModel(){", 1)[0]
    assert "r.note" not in render
    assert "Bez notatki" not in render
    assert "date(r.ts)" in render
    assert "n(r.kg,1)+' kg'" in render


def test_telemetry_quality_panel_uses_read_only_audit_endpoint_and_handles_missing_data():
    source = (ROOT / "assets/js/fuel-panel.js").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="fuelTelemetryQuality"' in html
    assert "/api/fuel/telemetry-audit?days=30" in source
    assert "audit?.raw_ingestion_quality||null" in source
    assert "timestamp_unusable_samples" in source
    assert "duplicate_timestamp_feeds" in source
    assert "loss_percent" in source
    assert "Audyt read-only" in source
    assert "fuelTelemetryQuality" in source


def test_fuel_chart_does_not_draw_zero_or_negative_counter_values():
    source = (ROOT / "assets/js/fuel-panel.js").read_text(encoding="utf-8")
    assert "if(!Number.isFinite(v)||v<=0)return" in source


def test_weather_aware_pellet_forecast_has_one_canonical_fuel_location():
    """Live kg/day forecast belongs to Fuel; Diagnostics keeps historical benchmark only."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    intelligence = (ROOT / "assets/js/intelligence-panel.js").read_text(encoding="utf-8")
    fuel_panel = (ROOT / "assets/js/fuel-panel.js").read_text(encoding="utf-8")
    assert 'id="fuelForecast"' in html
    assert "function renderForecast()" in fuel_panel
    assert "weatherForecast" not in intelligence
    assert "data?.weather_forecast" not in intelligence
    assert "Wpływ pogody · test historyczny" in intelligence
    assert "Prognoza peletu i pogody jest dostępna wyłącznie w zakładce" in html
    assert "data-view-target=\"overview\" aria-selected=\"true\">Przegląd" in html
    assert "data-view-target=\"fuel\" aria-selected=\"false\">Paliwo i prognoza" in html


def test_production_acceptance_card_is_present_and_versioned():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "assets/js/acceptance-panel.js").read_text(encoding="utf-8")
    css = (ROOT / "assets/css/components.css").read_text(encoding="utf-8")
    assert 'id="acceptanceCard"' in html
    assert 'id="acceptanceSummary"' in html
    assert 'id="acceptanceGates"' in html
    assert 'id="acceptanceRefresh"' in html
    assert "assets/js/acceptance-panel.js" in html
    assert "acceptance-panel.js?v=__APP_COMMIT__" in html
    assert "/api/thermoflow/v1/acceptance" in script
    assert "thermoflow-production-acceptance/2" in script
    assert "Brak danych nie oznacza zaliczenia" in html
    assert "55°C ±1°C" in html
    assert ".acceptance-gates" in css


def test_production_acceptance_card_never_treats_unknown_as_passed():
    script = (ROOT / "assets/js/acceptance-panel.js").read_text(encoding="utf-8")
    assert "Nieznany status" in script
    assert "Odbiór niedostępny" in script
    assert "Brak danych nie oznacza zaliczenia" in script
    assert "ready_for_manual_signoff" in script
    assert "automatic_signoff" not in script


def test_fuel_analytics_layout_keeps_mobile_panels_full_width():
    css = (ROOT / "assets/css/fuel.css").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'class="fuel-analysis-layout"' in html
    assert 'class="fuel-analysis-main"' in html
    assert 'class="fuel-analysis-side"' in html
    assert ".fuel-analysis-layout{" in css
    assert "grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr)" in css
    assert "@media(max-width:760px)" in css
    assert "grid-template-columns:minmax(0,1fr)" in css
    assert ".fuel-analysis-layout .fuel-detail-panel" in css


def test_digital_twin_is_rendered_in_acceptance_card():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "assets/js/acceptance-panel.js").read_text(encoding="utf-8")
    css = (ROOT / "assets/css/components.css").read_text(encoding="utf-8")
    assert 'id="acceptanceTwin"' in html
    assert "renderTwin(data.digital_twin)" in script
    assert "Ekstrapolacja trendu pomiarów" in script
    assert "acceptance-twin-grid" in script
    assert ".acceptance-twin" in css
    assert "heuristic_not_probability" not in script


def test_unified_intelligence_renders_digital_twin_trends():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "assets/js/intelligence-panel.js").read_text(encoding="utf-8")
    css = (ROOT / "assets/css/components.css").read_text(encoding="utf-8")
    assert 'id="intelligenceDigitalTwin"' in html
    assert "renderDigitalTwin(data?.digital_twin)" in script
    assert "Ekstrapolacja ostatniego trendu" in script
    assert "intelligence-twin-values" in script
    assert ".intelligence-digital-twin" in css

def test_fuel_mobile_telemetry_panel_cannot_collapse_to_narrow_column():
    css = (ROOT / "assets/css/ui-polish.css").read_text(encoding="utf-8")
    assert "@media(max-width:600px)" in css
    assert "#fuelCard .fuel-analysis-side{\n    display:grid;" in css
    assert "grid-column:1/-1;" in css
    assert "#fuelCard .fuel-telemetry-quality-panel .fuel-detail-heading h4" in css
    assert "#fuelCard .fuel-quality-fields{grid-template-columns:minmax(0,1fr)}" in css
    assert "overflow-wrap:anywhere" in css


def test_comfort_section_is_read_only_and_never_presents_causes_as_certain():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "assets/js/fibaro.js").read_text(encoding="utf-8")
    css = (ROOT / "assets/css/fibaro.css").read_text(encoding="utf-8")
    for element_id in ("comfortSection", "comfortBanner", "comfortList", "comfortNote", "comfortMeta"):
        assert f'id="{element_id}"' in html
    assert "/api/fibaro/comfort?hours=" in script
    assert "ThermoFlowApi.post" not in script          # the panel never writes
    assert "pewność:" in script and "comfort.limitations" in script
    assert "BRAK DANYCH" in script                      # unknown is shown as unknown, not as OK
    assert ".comfort-chip.unknown" in css and ".comfort-row" in css


def test_comfort_banner_never_reports_all_clear_when_zones_have_no_assessment():
    script = (ROOT / "assets/js/fibaro.js").read_text(encoding="utf-8")
    assert "Brak danych do oceny." in script
    assert "unknown_now.length>=s.zones" in script          # all zones unknown -> not "all clear"
    assert "Żadna z ocenianych stref" in script             # all-clear wording only covers assessed zones
    assert "Bez oceny (brak danych):" in script             # partially unknown zones are named
    assert "Żadna strefa nie jest teraz poniżej nastawy." not in script
