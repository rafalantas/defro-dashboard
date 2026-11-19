"""Static checks for the split ThermoFlow frontend."""
from html.parser import HTMLParser
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

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
                self.assets.append(path.lstrip("/"))
            if tag == "script" and "src" not in attrs:
                self.inline_scripts += 1
        if tag == "style":
            self.inline_styles += 1
        if tag == "svg" and attrs.get("class") == "schematic" and attrs.get("viewbox") == "0 0 1380 600":
            self.schematic = True

audit = Audit()
audit.feed(HTML)
assert len(audit.ids) >= 127, f"Expected all 127 baseline IDs to remain, got {len(audit.ids)}"
assert len(audit.ids) == len(set(audit.ids)), "Duplicate HTML IDs"
assert not audit.inline_scripts, "JavaScript must stay in external modules"
assert not audit.inline_styles, "CSS must stay in external stylesheets"
assert audit.schematic, "Original inline installation SVG must remain present"
for asset in audit.assets:
    assert (ROOT / asset).is_file(), f"Missing frontend asset: {asset}"
reference = (ROOT / "assets/js/reference-model.js").read_text(encoding="utf-8")
for expected in ("heatingKgPerHdd:4.49", "fixedDailyKg:5", "hddBaseC:15.5"):
    assert expected in re.sub(r"\s+", "", reference), f"Missing reference constant: {expected}"
print(f"Frontend assets: {len(audit.assets)} paths, {len(audit.ids)} stable IDs, inline SVG: OK")
