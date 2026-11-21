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
