"""Read-only projection of offline benchmarks and the existing live shadow view."""
import json
import os
import stat
from copy import deepcopy
from math import isfinite
from threading import Lock

from thermoflow_shadow_runtime import SCHEMA as SHADOW_SCHEMA

PATH = "/api/thermoflow/v1/intelligence"
SCHEMA = "thermoflow-unified-intelligence/1"
BUNDLE_SCHEMA = "thermoflow-intelligence-bundle/1"
MAX_BYTES = 64 * 1024
MAX_BUNDLE_AGE = 7 * 86400
REFRESH_SECONDS = 60
OFFLINE = {
    "burner_ml": ("fire_confirmed_cycles", "minutes", ("candidate", "training_median", "previous_cycle")),
    "fuel_ml": ("verified_refills_as_issued", "kg_per_day", ("candidate", "as_issued_baseline", "previous_period")),
    "weather_impact": ("verified_refills_archived_forecasts", "kg_per_day", ("without_weather", "with_weather")),
}


def _count(value):
    return type(value) is int and 0 <= value <= 10_000_000


def _metric(value):
    return type(value) in (float, int) and -1e12 <= value <= 1e12


def _summary(name, report):
    """Project only stable aggregate fields, never model weights or source rows."""
    source, unit, comparisons = OFFLINE[name]
    if not isinstance(report, dict) or report.get("source") != source:
        raise ValueError("invalid benchmark provenance")
    status, samples = report.get("status"), report.get("eligible_samples")
    if status not in ("evaluated", "collecting", "unavailable") or not _count(samples):
        raise ValueError("invalid benchmark availability")
    scores = None
    if status == "evaluated":
        raw = report.get("holdout")
        if samples < 25 or not isinstance(raw, dict) or set(raw) != set(comparisons):
            raise ValueError("insufficient or invalid holdout")
        scores = {}
        for variant in comparisons:
            score = raw[variant]
            if not isinstance(score, dict) or not _count(score.get("samples")) or score["samples"] != 5:
                raise ValueError("invalid holdout count")
            values = {metric: score.get(metric) for metric in ("mae", "rmse", "bias")}
            if not all(_metric(value) for value in values.values()) or min(values["mae"], values["rmse"]) < 0:
                raise ValueError("invalid holdout score")
            scores[variant] = {"samples": score["samples"], **values}
    return {"status": status, "source": source, "unit": unit,
            "eligible_samples": samples, "holdout": scores}


def validate_bundle(bundle):
    if not isinstance(bundle, dict) or bundle.get("schema") != BUNDLE_SCHEMA:
        raise ValueError("invalid intelligence bundle")
    published, modules = bundle.get("evaluated_at"), bundle.get("modules")
    if type(published) is not int or not 0 < published <= 253402300799:
        raise ValueError("invalid bundle time")
    if not isinstance(modules, dict) or not set(modules) <= OFFLINE.keys():
        raise ValueError("invalid bundle modules")
    clean = {}
    for name, report in modules.items():
        try:
            clean[name] = _summary(name, report)
        except ValueError:
            clean[name] = None  # An invalid module cannot hide valid sibling results.
    return published, clean


def _read_bundle(path):
    # Restrict to regular files; a misconfigured FIFO must not block the worker.
    flags = os.O_RDONLY | os.O_NONBLOCK
    with open(path, "rb", opener=lambda filename, _: os.open(filename, flags)) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("bundle must be a regular file")
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("bundle exceeds size limit")
    return validate_bundle(json.loads(raw))


def _offline_entry(status, reason, *, evaluated_at=None, summary=None):
    return {"mode": "offline_benchmark", "status": status, "reason": reason,
            "evaluated_at": evaluated_at, "result": summary, "prediction": None}


def _live_entries(shadow):
    valid = isinstance(shadow, dict) and shadow.get("schema") == SHADOW_SCHEMA
    state = shadow.get("status") if valid else "telemetry_unavailable"
    stamp = shadow.get("evaluated_at") if valid else None
    entries = {}
    for name, key, accepted in (
        ("health_score", "health", ("screened",)),
        ("diagnostic_ai", "diagnostics", ("review", "no_signal_in_observed_components")),
    ):
        report = shadow.get(key) if valid and state not in ("stale", "telemetry_unavailable") else None
        report_state = report.get("status") if isinstance(report, dict) else None
        available = report_state in accepted
        # Partial health can explain coverage without pretending to be a score.
        readable = available or (key == "health" and report_state == "unknown")
        entries[name] = {
            "mode": "live_advisory", "status": report_state if available else state,
            "reason": None if available else "insufficient_or_stale_telemetry",
            "evaluated_at": stamp, "result": deepcopy(report) if readable else None,
        }
    for name, key, accepted in (
        ("anomaly_v3", "anomaly_v3", ("available", "collecting")),
        ("causal_diagnostics", "causal_diagnostics", ("available", "collecting")),
    ):
        report = shadow.get(key) if valid and state not in ("stale", "telemetry_unavailable") else None
        report_state = report.get("status") if isinstance(report, dict) else None
        readable = report_state in accepted
        entries[name] = {
            "mode": "live_advisory",
            "status": report_state if readable else state,
            "reason": None if readable else "insufficient_or_stale_telemetry",
            "evaluated_at": stamp,
            "result": deepcopy(report) if readable else None,
        }
    screens = shadow.get("screens") if valid and state not in ("stale", "telemetry_unavailable") else None
    return entries, deepcopy(screens)


class UnifiedRuntime:
    def __init__(self, bundle_path=None):
        self._path = bundle_path
        self._lock = Lock()
        self._bundle = ("not_loaded" if bundle_path else "not_configured", None, {})

    def refresh(self, *, now):
        """Load outside the lock; publish one complete generation or fail closed."""
        if not self._path:
            return
        try:
            evaluated_at, modules = _read_bundle(self._path)
            if evaluated_at > now:
                raise ValueError("future bundle")
            loaded = ("loaded", evaluated_at, modules)
        except FileNotFoundError:
            loaded = ("missing_bundle", None, {})
        except (OSError, ValueError, RecursionError, OverflowError):
            loaded = ("invalid_bundle", None, {})
        with self._lock:
            self._bundle = loaded

    def read(self, now, shadow):
        with self._lock:
            state, stamp, summaries = self._bundle
        modules, screens = _live_entries(shadow)
        for name in OFFLINE:
            if state != "loaded":
                entry = _offline_entry("unavailable" if state == "invalid_bundle" else "collecting", state)
            elif not isfinite(now) or not 0 <= now - stamp <= MAX_BUNDLE_AGE:
                entry = _offline_entry("stale", "bundle_outside_freshness_window", evaluated_at=stamp)
            elif name not in summaries:
                entry = _offline_entry("collecting", "no_benchmark_report", evaluated_at=stamp)
            elif summaries[name] is None:
                entry = _offline_entry("unavailable", "invalid_benchmark_report", evaluated_at=stamp)
            else:
                report = summaries[name]
                status = report["status"]
                reason = {"collecting": "insufficient_verified_samples",
                          "unavailable": "no_usable_benchmark"}.get(status)
                entry = _offline_entry(status, reason, evaluated_at=stamp, summary=deepcopy(report))
            modules[name] = entry
        return {"schema": SCHEMA, "mode": "advisory_only", "promotion": "disabled",
                "live_predictions_enabled": False, "modules": modules, "screens": screens}
