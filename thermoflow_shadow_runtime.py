"""Bounded, volatile diagnostics driven by the existing API poller."""
from collections import deque
from datetime import datetime
from math import isfinite
from threading import Lock

from thermoflow_anomaly_v2 import screen_series
from thermoflow_anomaly_v3 import analyze_multisignal
from thermoflow_diagnostic_ai import diagnostic_hypotheses
from thermoflow_health_score import build_health_score

PATH = "/api/thermoflow/v1/shadow"
SCHEMA = "thermoflow-shadow/1"
MAX_AGE = 90
MAX_GAP = 75
COMPONENTS = {"flue": 8.0, "return": 3.0}


def _signal(value):
    if type(value) in (bool, int, float) and value in (0, 1):
        return bool(value)
    return None


def _envelope(status, *, timestamp=None, **details):
    return {"schema": SCHEMA, "mode": "advisory_only", "source": "emodul_poller_ram",
            "status": status, "evaluated_at": timestamp, **details}


class ShadowRuntime:
    """One writer (the poller); readers never take the I/O/cache lock."""

    def __init__(self):
        self._lock = Lock()
        self._rows = deque(maxlen=49)
        self._anomaly_rows = deque(maxlen=241)
        self._regime = None
        self._timestamp = None
        self._report = _envelope("collecting")

    def _publish(self, report):
        with self._lock:
            self._report = report

    def observe(self, payload):
        try:
            if payload.get("connected") is not True:
                raise ValueError
            fetched = datetime.fromisoformat(payload["fetched_at"])
            if fetched.utcoffset() is None:
                raise ValueError
            timestamp = int(fetched.timestamp())
            data = payload["data"]
            regime = (_signal(data.get("fire")),
                      *(_signal(data["pumps"].get(key)) for key in ("valve4d", "floor", "dhw")))
            if None in regime or timestamp <= 0:
                raise ValueError
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            self._rows.clear()
            self._anomaly_rows.clear()
            self._regime = None
            self._publish(_envelope("telemetry_unavailable"))
            return
        if self._timestamp is not None and timestamp <= self._timestamp:
            return  # Cached (or out-of-order) reads are not new samples.
        gap = timestamp - self._timestamp if self._timestamp is not None else None
        if self._regime != regime or (gap is not None and gap > MAX_GAP):
            self._rows.clear()
        if gap is not None and gap > 75:
            self._anomaly_rows.clear()
        self._regime, self._timestamp = regime, timestamp
        temperatures = data.get("temperatures") or {}
        valve = data.get("valve") or {}
        pumps = data.get("pumps") or {}
        self._rows.append((timestamp, temperatures.get("flue"), valve.get("return")))
        self._anomaly_rows.append({
            "timestamp": timestamp,
            "return_c": valve.get("return"),
            "boiler_c": temperatures.get("co"),
            "flue_c": temperatures.get("flue"),
            "valve_opening_pct": valve.get("opening"),
            "pumps": {name: _signal(pumps.get(name)) for name in
                      ("floor", "dhw", "valve4d", "circulation")},
        })
        anomaly = analyze_multisignal(tuple(self._anomaly_rows), as_of=timestamp)
        screens = {
            name: screen_series(((row[0], row[i]) for row in self._rows),
                                as_of=timestamp, max_gap=MAX_GAP, max_age=MAX_AGE,
                                minimum_deviation=minimum)
            for i, (name, minimum) in enumerate(COMPONENTS.items(), start=1)
        }
        health = build_health_score(screens, expected=COMPONENTS, as_of=timestamp)
        diagnostics = diagnostic_hypotheses(health, screens)
        if health["status"] == "screened":
            state = "screened"
        elif any(screen["data_state"] == "ambiguous_latest" for screen in screens.values()):
            state = "telemetry_unavailable"
        else:
            state = "collecting"
        self._publish(_envelope(state, timestamp=timestamp, screens=screens,
                                health=health, diagnostics=diagnostics,
                                anomaly_v3=anomaly,
                                causal_diagnostics=anomaly.get("causal_diagnostics", {
                                    "status": "collecting", "findings": [],
                                    "causality": "not_established"})))

    def read(self, now):
        with self._lock:
            report = self._report
        timestamp = report["evaluated_at"]
        if timestamp is not None and (not isfinite(now) or not 0 <= now - timestamp <= MAX_AGE):
            return _envelope("stale", timestamp=timestamp)
        return report
