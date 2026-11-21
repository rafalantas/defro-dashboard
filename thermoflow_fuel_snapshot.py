"""Build an offline Fuel ML dataset from existing read-only ThingSpeak feeds."""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from statistics import median
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from fuel_config import (
    COUNTER_EPOCH, FIELD_COUNTER, REFILL_MERGE_SECONDS, TS_CHANNEL_FUEL,
    TS_CHANNEL_VALVE, TS_READ_KEY_FUEL, TS_READ_KEY_VALVE,
)
from fuel_refills import merge_close_refills, normalize_refill_feeds
from thermoflow_fuel_ml import AS_ISSUED, INPUT_SCHEMA, REPLAY, evaluate_fuel_model

LIMIT = 8000
MAX_COUNTER_WINDOWS = 80
COUNTER_RADIUS = timedelta(minutes=10)
RESET_RISE_PP = 2


class SnapshotError(Exception):
    """Safe to print; does not contain credentials or query strings."""


def _stamp(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.utcoffset() is None:
        raise ValueError("timezone required")
    return date.astimezone(timezone.utc)


def _read(channel, key, *, field=None, bounds=None, opener=urlopen):
    if not channel.isascii() or not channel.isdigit() or not key:
        raise SnapshotError("existing ThingSpeak channel ID and read key are required")
    params = {"api_key": key, "results": LIMIT, "timezone": "Etc/UTC"}
    if bounds:
        params.update(zip(("start", "end"),
                          (value.strftime("%Y-%m-%d %H:%M:%S") for value in bounds)))
    suffix = "feeds" if field is None else f"fields/{field}"
    url = f"https://api.thingspeak.com/channels/{channel}/{suffix}.json?{urlencode(params)}"
    try:
        with opener(url, timeout=15) as response:
            rows = json.load(response)["feeds"]
    except HTTPError as error:
        raise SnapshotError(f"ThingSpeak HTTP {error.code}") from None
    except Exception as error:
        raise SnapshotError(f"ThingSpeak read failed ({type(error).__name__})") from None
    if not isinstance(rows, list) or len(rows) >= LIMIT:
        raise SnapshotError("invalid or potentially truncated ThingSpeak feed")
    return rows


def _counter_context(rows, start, end, as_of):
    """Boundary context only. Controller % cannot verify physical inventory."""
    buckets = {}
    for row in rows:
        try:
            timestamp = _stamp(row["created_at"])
            percent = float(row[f"field{FIELD_COUNTER}"])
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
        if start - COUNTER_RADIUS <= timestamp <= min(end + COUNTER_RADIUS, as_of) and 0 <= percent <= 100:
            buckets.setdefault(timestamp, set()).add(percent)
    last_before = max((t for t in buckets if t < start), default=None)
    first_after = min((t for t in buckets if t > end), default=None)
    before = buckets.get(last_before, set())
    after = buckets.get(first_after, set())
    if len(before) != 1 or len(after) != 1:
        return {"status": "missing_or_ambiguous_bracket"}
    before_value, after_value = next(iter(before)), next(iter(after))
    return {
        "status": "controller_rise" if after_value - before_value >= RESET_RISE_PP else "no_clear_rise",
        "before_percent": before_value, "after_percent": after_value,
        "before_at": last_before.isoformat(), "after_at": first_after.isoformat(),
    }


def _groups(rows, as_of, start):
    """Normalize the complete refill feed first, including later corrections."""
    try:
        observed = [row for row in rows if _stamp(row["created_at"]) <= as_of]
        for row in observed:
            if row.get("field1") not in (None, "") and int(row["entry_id"]) <= 0:
                raise ValueError
        clean = normalize_refill_feeds(observed, timezone.utc, _stamp)
        if (any(not isfinite(row["kg"]) or row["kg"] <= 0 for row in clean)
                or len({row["id"] for row in clean}) != len(clean)):
            raise ValueError
        merged = merge_close_refills(clean, REFILL_MERGE_SECONDS, _stamp)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        # Skipping a broken refill could incorrectly connect two other fills.
        raise SnapshotError("invalid refill ledger; cannot safely join intervals") from None
    prior = {_row["id"]: _stamp(_row["ts"]) for _row in clean}
    return [
        {**{key: group[key] for key in ("ids", "kg", "ts")},
         "first_at": prior[group["ids"][0]]}
        for group in merged
        if prior[group["ids"][0]] >= start
    ]


def build_snapshot(rows, *, start, as_of, epochs, verified_ids=(), assume_equal_fill=False,
                   archived_baselines=None, counter_reader=None):
    if start >= as_of or start.utcoffset() is None or as_of.utcoffset() is None:
        raise ValueError("invalid snapshot range")
    if assume_equal_fill and verified_ids:
        raise ValueError("assumed and verified scenarios must be run separately")
    groups = _groups(rows, as_of, start)
    verified_ids = set(verified_ids)
    if counter_reader is not None and len(groups) > MAX_COUNTER_WINDOWS:
        raise SnapshotError("too many counter windows; shorten the range or use --without-counter")
    for group in groups:
        first, last = group.pop("first_at"), _stamp(group["ts"])
        group["counter"] = (_counter_context(
                                counter_reader(first - COUNTER_RADIUS, min(as_of, last + COUNTER_RADIUS)),
                                first, last, as_of)
                            if counter_reader is not None else {"status": "not_read"})
        group["verified_equal_fill"] = all(entry in verified_ids for entry in group["ids"])
    epochs = sorted({_stamp(epoch) for epoch in epochs})
    periods, prior = [], []
    active_regime = None
    label_mode = "assumed_equal_fill_level" if assume_equal_fill else "verified_equal_fill_level"
    for left, right in zip(groups, groups[1:]):
        begin, end = _stamp(left["ts"]), _stamp(right["ts"])
        regime = bisect_right(epochs, begin)
        if regime != active_regime:
            prior, active_regime = [], regime
        accepted = assume_equal_fill or (left["verified_equal_fill"] and right["verified_equal_fill"])
        row = {"start": left["ts"], "end": right["ts"], "refilled_kg": right["kg"],
               "start_refill_ids": left["ids"], "end_refill_ids": right["ids"],
               "mass_reference": label_mode if accepted else "unverified"}
        if archived_baselines is None:
            if prior:
                row.update(baseline_kg_per_day=median(prior), baseline_replay_cutoff=left["ts"])
        else:
            saved = archived_baselines.get(begin)
            if saved:
                row.update(baseline_kg_per_day=saved["baseline_kg_per_day"],
                           baseline_issued_at=saved["baseline_issued_at"])
        crosses = regime < len(epochs) and epochs[regime] < end
        if accepted and not crosses and end > begin:
            prior.append(right["kg"] / ((end - begin).total_seconds() / 86400))
        periods.append(row)

    return {
        "schema": INPUT_SCHEMA, "source": "existing_thingspeak_refills",
        "baseline_mode": REPLAY if archived_baselines is None else AS_ISSUED, "label_mode": label_mode,
        "as_of": as_of.isoformat(), "range_start": start.isoformat(),
        "calibration_epochs": [epoch.isoformat() for epoch in epochs],
        "refills": groups, "periods": periods,
    }


def _metadata(path):
    if path is None:
        return (), None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != "thermoflow-fuel-import-metadata/1":
        raise ValueError
    verified = doc.get("verified_refill_ids", [])
    if not isinstance(verified, list) or any(type(value) is not int or value <= 0 for value in verified):
        raise ValueError
    if "archived_baselines" not in doc:
        return verified, None
    baselines = {}
    for item in doc["archived_baselines"]:
        ts = _stamp(item["start"])
        if ts in baselines:
            raise ValueError
        # A late or invalid issued baseline is rejected by the benchmark.
        baselines[ts] = {"baseline_issued_at": _stamp(item["baseline_issued_at"]).isoformat(),
                         "baseline_kg_per_day": item["baseline_kg_per_day"]}
    return verified, baselines


def main(argv=None, *, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=120, choices=range(1, 366), metavar="1..365")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--assume-equal-fill", action="store_true",
                        help="separate estimated scenario, not verified fuel consumption")
    parser.add_argument("--without-counter", action="store_true")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--status", action="store_true", help="counts and states only")
    output.add_argument("--evaluate", action="store_true", help="run the offline fuel benchmark")
    args = parser.parse_args(argv)
    now = now if now is not None else datetime.now(timezone.utc)
    try:
        verified, baselines = _metadata(args.metadata)
        snapshot = build_snapshot(
            _read(TS_CHANNEL_FUEL, TS_READ_KEY_FUEL),
            start=now - timedelta(days=args.days), as_of=now,
            epochs=[COUNTER_EPOCH.isoformat()], verified_ids=verified,
            assume_equal_fill=args.assume_equal_fill, archived_baselines=baselines,
            counter_reader=(None if args.without_counter else
                            lambda start, end: _read(TS_CHANNEL_VALVE, TS_READ_KEY_VALVE,
                                                     field=FIELD_COUNTER, bounds=(start, end))),
        )
        report = evaluate_fuel_model(snapshot) if args.evaluate or args.status else None
        if args.status:
            result = {
                "refills": len(snapshot["refills"]), "periods": len(snapshot["periods"]),
                "counter_states": dict(Counter(row["counter"]["status"] for row in snapshot["refills"])),
                "status": report["status"], "reason": report.get("reason"),
                "label_mode": report["label_mode"], "baseline_mode": report["baseline_mode"],
                "eligible_samples": report["dataset"]["eligible_samples"],
                "excluded_periods_by_reason": report["excluded_periods_by_reason"],
            }
        else:
            result = report if args.evaluate else snapshot
        output = json.dumps(result, allow_nan=False, indent=2)
    except SnapshotError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (KeyError, ValueError, TypeError, OSError, AttributeError, OverflowError):
        print("invalid import metadata, configuration or data", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
