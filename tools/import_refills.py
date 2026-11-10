#!/usr/bin/env python3
"""
Jednorazowy import historii dosypek peletu do kanału ThingSpeak "Pelet".

Plik wejściowy: wiersze "DD.MM.YYYY<TAB, ; lub ,>kg", np. skopiowane z Numbers:
    20.04.2026	240
    28.04.2026	180
Wiersze w innym formacie (nagłówki, ekogroszek z dopiskiem itp.) są pomijane,
chyba że podasz --from, wtedy brane są tylko daty od tego dnia.

Godzina każdego wpisu: 12:00 czasu lokalnego (w arkuszu jest tylko data).

Użycie:
    export THINGSPEAK_FUEL_CHANNEL_ID=1234567
    export THINGSPEAK_FUEL_WRITE_KEY=XXXXXXXXXXXXXXXX
    python3 import_refills.py dosypki.txt              # podgląd, nic nie wysyła
    python3 import_refills.py dosypki.txt --send       # wysyła do ThingSpeak

Uruchom import, zanim dodasz pierwszą dosypkę przez API lub Skrót.
"""
import argparse
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TZ = ZoneInfo("Europe/Warsaw")
LINE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*[\t;,]\s*(\d+(?:[.,]\d+)?)\s*(.*)$")


def parse(path: str, date_from: date | None):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            m = LINE_RE.match(line)
            if not m:
                if line.strip():
                    print(f"  pomijam wiersz {n}: {line.strip()}")
                continue
            d, mo, y, kg, rest = m.groups()
            day = date(int(y), int(mo), int(d))
            if date_from and day < date_from:
                continue
            dt = datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
            rows.append((dt, float(kg.replace(",", ".")), rest.strip()[:255]))
    rows.sort(key=lambda r: r[0])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--from", dest="date_from", help="pierwsza data do importu, DD.MM.YYYY")
    ap.add_argument("--send", action="store_true", help="wyślij do ThingSpeak (bez tego tylko podgląd)")
    args = ap.parse_args()

    date_from = datetime.strptime(args.date_from, "%d.%m.%Y").date() if args.date_from else None
    rows = parse(args.file, date_from)
    if not rows:
        sys.exit("Brak wierszy do importu")

    print(f"\n{len(rows)} wpisów:")
    for dt, kg, note in rows:
        print(f"  {dt.isoformat()}  {kg:7.1f} kg  {note}")
    print("\nPierwszy wpis to punkt startowy (nie liczy się jako zużycie).")

    if not args.send:
        print("\nPodgląd. Dodaj --send, żeby wysłać.")
        return

    channel = os.environ.get("THINGSPEAK_FUEL_CHANNEL_ID")
    key = os.environ.get("THINGSPEAK_FUEL_WRITE_KEY")
    if not (channel and key):
        sys.exit("Ustaw THINGSPEAK_FUEL_CHANNEL_ID i THINGSPEAK_FUEL_WRITE_KEY")

    updates = []
    for dt, kg, note in rows:
        u = {"created_at": dt.isoformat(), "field1": kg}
        u["field2"] = note or "import"
        updates.append(u)

    req = Request(
        f"https://api.thingspeak.com/channels/{channel}/bulk_update.json",
        data=json.dumps({"write_api_key": key, "updates": updates}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            print(f"\nThingSpeak: HTTP {resp.status} {resp.read().decode()}")
    except HTTPError as e:
        sys.exit(f"\nThingSpeak: HTTP {e.code} {e.read().decode()}")

if __name__ == "__main__":
    main()
