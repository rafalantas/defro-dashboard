"""In-process, single-flight runner for the explicit read-only telemetry audit."""
from __future__ import annotations

import threading
from datetime import datetime, timezone


class TelemetryAuditJob:
    """Run one audit in a daemon thread and retain its latest result in memory."""

    def __init__(self, audit):
        self._audit = audit
        self._lock = threading.Lock()
        self._state = "never_run"
        self._started_at = None
        self._finished_at = None
        self._result = None
        self._error = None

    def status(self):
        with self._lock:
            return self._snapshot_locked()

    def start(self):
        with self._lock:
            if self._state == "running":
                return self._snapshot_locked()
            self._state = "running"
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._finished_at = None
            self._error = None
            thread = threading.Thread(target=self._run, name="telemetry-quality-audit", daemon=True)
            thread.start()
            return self._snapshot_locked()

    def _snapshot_locked(self):
        return {"state": self._state, "started_at": self._started_at,
                "finished_at": self._finished_at, "result": self._result,
                "error": self._error}

    def _run(self):
        try:
            result = self._audit()
        except Exception as error:
            with self._lock:
                self._state = "failed"
                self._finished_at = datetime.now(timezone.utc).isoformat()
                self._error = f"{type(error).__name__}: {getattr(error, 'message', str(error))}"[:240]
            return
        with self._lock:
            self._result = result
            self._state = "completed"
            self._finished_at = datetime.now(timezone.utc).isoformat()
            self._error = None
