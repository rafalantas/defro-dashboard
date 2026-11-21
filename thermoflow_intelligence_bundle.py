"""Build a public aggregate bundle offline using the existing ThermoFlow benchmarks."""
import argparse
import json
import sys
from pathlib import Path
from time import time

from thermoflow_burner_ml import evaluate_burner_model
from thermoflow_fuel_ml import _instant, evaluate_fuel_model
from thermoflow_observed_cycles import build_observed_cycle_dataset
from thermoflow_unified_runtime import BUNDLE_SCHEMA, MAX_BYTES, OFFLINE, validate_bundle
from thermoflow_weather_impact import evaluate_weather_impact

MAX_INPUT_BYTES = 16 * 1024 * 1024


def _read_input(path):
    with path.open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    return json.loads(raw)


def _archive_is_verified(doc, now):
    if (not isinstance(doc, dict) or doc.get("label_mode", "verified_equal_fill_level") != "verified_equal_fill_level"
            or doc.get("baseline_mode", "as_issued") != "as_issued"):
        raise ValueError("expected verified refills and archived as-issued baselines")
    for period in doc["periods"]:
        try:
            end = _instant(period["end"])
        except (KeyError, ValueError, TypeError, OSError, OverflowError):
            continue  # The original benchmark rejects structurally invalid rows.
        if end > now:
            raise ValueError("refill archive contains future outcomes")


def _metrics(scores, unit):
    return {
        name: {"samples": score["samples"], **{key: score[f"{key}_{unit}"] for key in ("mae", "rmse", "bias")}}
        for name, score in scores.items()
    }


def _run(name, doc, now):
    source, unit, _ = OFFLINE[name]
    if name == "burner_ml":
        observed = build_observed_cycle_dataset(doc)
        if (observed["last_fire_timestamp"] or 0) > now:
            raise ValueError("FIRE snapshot contains future observations")
        result = evaluate_burner_model(observed)
        count = result["eligible_samples"]
        scores = ({**result["evaluation"]["baselines"], "candidate": result["evaluation"]["candidate"]}
                  if result["status"] == "evaluated" and result["model"] is not None else None)
    else:
        _archive_is_verified(doc, now)
        if name == "fuel_ml":
            result = evaluate_fuel_model(doc)
            count = result["dataset"]["eligible_samples"]
            scores = result["evaluation"] if result["status"] == "evaluated" and result["model"] is not None else None
        else:
            result = evaluate_weather_impact(doc)
            count = result["paired"]
            scores = result["evaluation"] if result["status"] == "evaluated" else None
    status = "unavailable" if result["status"] == "evaluated" and not scores else result["status"]
    return {"source": source, "status": status, "eligible_samples": count,
            "holdout": _metrics(scores, unit) if scores else None}


def build_bundle(inputs, *, now):
    if not inputs or not set(inputs) <= OFFLINE.keys():
        raise ValueError("provide at least one supported benchmark input")
    bundle = {"schema": BUNDLE_SCHEMA, "evaluated_at": now,
              "modules": {name: _run(name, doc, now) for name, doc in inputs.items()}}
    _, modules = validate_bundle(bundle)
    if None in modules.values():
        raise ValueError("benchmark produced malformed metrics")
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--burner", type=Path, help="raw FIRE snapshot")
    parser.add_argument("--fuel", type=Path, help="verified periods with as-issued baselines")
    parser.add_argument("--weather", type=Path, help="verified periods with archived forecasts")
    args = parser.parse_args(argv)
    try:
        inputs = {name: _read_input(path) for name, path in
                  zip(OFFLINE, (args.burner, args.fuel, args.weather), strict=True) if path is not None}
        output = json.dumps(build_bundle(inputs, now=int(time())), indent=2, allow_nan=False)
        if len(output.encode("utf-8")) > MAX_BYTES:
            raise ValueError("bundle exceeds size limit")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        print("Cannot prepare intelligence bundle: check sources, provenance and sample dates.", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
