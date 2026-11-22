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
                   archived_baselines=None, archived_forecasts=None, counter_reader=None):
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

    # Keep only forecasts captured for the exact refill interval and available
    # no later than its start. Missing historical forecasts are never inferred.
    period_ends = {_stamp(row["start"]): _stamp(row["end"]) for row in periods}
    forecasts, seen_forecasts = [], set()
    if archived_forecasts is None:
        archived_forecasts = []
    if not isinstance(archived_forecasts, list):
        raise ValueError("archived forecasts must be a list")
    for item in archived_forecasts:
        if not isinstance(item, dict):
            raise ValueError("invalid archived forecast record")
        forecast_start, forecast_end = _stamp(item["start"]), _stamp(item["end"])
        issued, available = _stamp(item["issued_at"]), _stamp(item["available_at"])
        value = item["forecast_mean_outdoor_c"]
        if forecast_start in seen_forecasts:
            raise ValueError("duplicate archived forecast period")
        if (period_ends.get(forecast_start) != forecast_end
                or item.get("kind") != "forecast"
                or not issued <= available <= forecast_start
                or type(value) not in (int, float) or not isfinite(value)
                or not -90 <= value <= 65):
            raise ValueError("archived forecast is invalid or not available at period start")
        seen_forecasts.add(forecast_start)
        forecasts.append({
            "start": forecast_start.isoformat(), "end": forecast_end.isoformat(),
            "issued_at": issued.isoformat(), "available_at": available.isoformat(),
            "kind": "forecast", "forecast_mean_outdoor_c": float(value),
        })

    return {
        "schema": INPUT_SCHEMA, "source": "existing_thingspeak_refills",
        "baseline_mode": REPLAY if archived_baselines is None else AS_ISSUED, "label_mode": label_mode,
        "as_of": as_of.isoformat(), "range_start": start.isoformat(),
        "calibration_epochs": [epoch.isoformat() for epoch in epochs],
        "refills": groups, "periods": periods, "forecasts": forecasts,
    }


def _metadata(path):
    """Load provenance metadata without silently accepting ambiguous records."""
    if path is None:
        return (), None, []
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != "thermoflow-fuel-import-metadata/1":
        raise ValueError("invalid fuel import metadata schema")
    verified = doc.get("verified_refill_ids", [])
    if (not isinstance(verified, list)
            or any(type(value) is not int or value <= 0 for value in verified)
            or len(set(verified)) != len(verified)):
        raise ValueError("verified refill IDs must be unique positive integers")
    if "archived_baselines" not in doc and "archived_forecasts" not in doc:
        return verified, None, []
    baselines = None
    if "archived_baselines" in doc:
        archive = doc["archived_baselines"]
        if not isinstance(archive, list):
            raise ValueError("archived baselines must be a list")
        baselines = {}
    for item in (archive if baselines is not None else []):
        if not isinstance(item, dict):
            raise ValueError("invalid archived baseline record")
        ts = _stamp(item["start"])
        issued = _stamp(item["baseline_issued_at"])
        rate = item["baseline_kg_per_day"]
        if ts in baselines:
            raise ValueError("duplicate archived baseline period")
        if issued > ts:
            raise ValueError("archived baseline was issued after its period started")
        if type(rate) not in (int, float) or not isfinite(rate) or rate <= 0:
            raise ValueError("archived baseline rate must be positive and finite")
        baselines[ts] = {"baseline_issued_at": issued.isoformat(),
                         "baseline_kg_per_day": float(rate)}
    forecasts = doc.get("archived_forecasts", [])
    if not isinstance(forecasts, list):
        raise ValueError("archived forecasts must be a list")
    return verified, baselines, forecasts

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
        verified, baselines, forecasts = _metadata(args.metadata)
        snapshot = build_snapshot(
            _read(TS_CHANNEL_FUEL, TS_READ_KEY_FUEL),
            start=now - timedelta(days=args.days), as_of=now,
            epochs=[COUNTER_EPOCH.isoformat()], verified_ids=verified,
            assume_equal_fill=args.assume_equal_fill, archived_baselines=baselines,
            archived_forecasts=forecasts,
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
