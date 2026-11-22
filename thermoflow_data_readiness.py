"""Offline, read-only readiness audit for real ThermoFlow ML snapshots."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thermoflow_burner_ml import evaluate_burner_model
from thermoflow_fuel_ml import evaluate_fuel_model
from thermoflow_fire_snapshot import FIRE, summarize_snapshot
from thermoflow_observed_cycles import build_observed_cycle_dataset
from thermoflow_weather_impact import evaluate_weather_impact

SCHEMA = "thermoflow-data-readiness/1"
MIN_ELIGIBLE = 25
HOLDOUT = 5


def _module(status, samples, *, reason=None, details=None):
    return {"status": status, "eligible_samples": samples,
            "required_samples": MIN_ELIGIBLE,
            "holdout_samples": HOLDOUT if status == "evaluated" else 0,
            "reason": reason, "details": details or {}}


def build_readiness_report(*, burner=None, fuel=None, weather=None):
    """Audit supplied snapshots; return aggregate evidence, never raw records."""
    if burner is None and fuel is None and weather is None:
        raise ValueError("provide at least one of burner, fuel or weather snapshot")
    modules, sources = {}, {}

    if burner is not None:
        observed = build_observed_cycle_dataset(burner)
        evaluated = evaluate_burner_model(observed)
        samples = evaluated.get("eligible_samples", observed["dataset"]["eligible_samples"])
        modules["burner_ml"] = _module(
            evaluated.get("status", "unavailable"), samples,
            reason=evaluated.get("reason"),
            details={
                "complete_cycles": observed["counts"].get("complete", 0),
                "incomplete_cycles": observed["counts"].get("incomplete", 0),
                "segments": observed["counts"].get("segments", 0),
                "input_quality": observed.get("input_quality", {}),
                "dataset_rejections": observed["dataset"].get("rejected_samples", {}),
            })
        summary = summarize_snapshot(burner)
        sources["burner"] = {
            "schema": burner.get("schema"), "sampling": burner.get("sampling"),
            "samples": summary["samples"], "first_timestamp": summary["first_timestamp"],
            "last_timestamp": summary["last_timestamp"],
            "invalid_or_missing": summary["invalid_or_missing"],
        }

    if fuel is not None:
        evaluated = evaluate_fuel_model(fuel)
        dataset = evaluated.get("dataset", {})
        modules["fuel_ml"] = _module(
            evaluated.get("status", "unavailable"), dataset.get("eligible_samples", 0),
            reason=evaluated.get("reason"),
            details={
                "source_periods": evaluated.get("source_periods", len(fuel.get("periods", []))),
                "baseline_mode": evaluated.get("baseline_mode", fuel.get("baseline_mode")),
                "label_mode": evaluated.get("label_mode", fuel.get("label_mode")),
                "excluded_periods": evaluated.get("excluded_periods_by_reason", {}),
                "dataset_rejections": dataset.get("rejected_samples", {}),
                "range_start": fuel.get("range_start"), "as_of": fuel.get("as_of"),
            })
        sources["fuel"] = {
            "schema": fuel.get("schema"), "periods": len(fuel.get("periods", [])),
            "refills": len(fuel.get("refills", [])),
            "baseline_mode": fuel.get("baseline_mode"), "label_mode": fuel.get("label_mode"),
            "range_start": fuel.get("range_start"), "as_of": fuel.get("as_of"),
        }

    if weather is not None:
        evaluated = evaluate_weather_impact(weather)
        modules["weather_impact"] = _module(
            evaluated.get("status", "unavailable"), evaluated.get("paired", 0),
            reason=evaluated.get("reason"),
            details={
                "fuel_eligible_samples": evaluated.get("fuel_eligible", 0),
                "archived_forecasts": len(weather.get("forecasts", [])),
                "paired_periods": evaluated.get("paired", 0),
                "range_start": weather.get("range_start"), "as_of": weather.get("as_of"),
            })
        sources["weather"] = {
            "schema": weather.get("schema"), "periods": len(weather.get("periods", [])),
            "archived_forecasts": len(weather.get("forecasts", [])),
            "baseline_mode": weather.get("baseline_mode"), "label_mode": weather.get("label_mode"),
        }

    statuses = [item["status"] for item in modules.values()]
    overall = ("ready" if statuses and all(s == "evaluated" for s in statuses)
               else "unavailable" if "unavailable" in statuses else "collecting")
    return {
        "schema": SCHEMA, "mode": "offline_read_only_audit", "status": overall,
        "promotion": "disabled", "live_predictions_enabled": False,
        "modules": modules, "sources": sources,
        "limitations": [
            "Audit validates supplied snapshots, not the physical provenance of source data.",
            "A successful historical holdout is not a live prediction or production model.",
            "Missing source files are not interpreted as zero consumption or zero boiler activity.",
            "No controller settings or boiler outputs are read or changed.",
        ],
    }


def _read(path):
    if path is None:
        return None
    with path.open("rb") as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("snapshot exceeds 16 MiB")
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--burner", type=Path, help="raw FIRE snapshot JSON")
    parser.add_argument("--fuel", type=Path, help="verified refill-period snapshot JSON")
    parser.add_argument("--weather", type=Path, help="verified refill periods plus archived forecasts JSON")
    args = parser.parse_args(argv)
    try:
        report = build_readiness_report(
            burner=_read(args.burner), fuel=_read(args.fuel), weather=_read(args.weather))
        print(json.dumps(report, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        print("Cannot audit ThermoFlow data: check snapshot schemas, provenance and values.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
