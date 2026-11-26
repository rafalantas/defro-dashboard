"""Live, read-only inference for the Burner ML and Fuel ML artifacts."""
from __future__ import annotations

import json
import sqlite3
import math
import os
import stat
import time
from datetime import datetime
from threading import Lock

from thermoflow_burner_live import load_live_artifact as load_burner_artifact
from thermoflow_burner_live import predict_cycle_duration
from thermoflow_fuel_live import load_live_artifact as load_fuel_artifact
from thermoflow_fuel_live import predict_fuel_rate
from thermoflow_burner_artifact_store import load_burner_model_artifact
from thermoflow_store import SQLiteStore

MAX_ARTIFACT_BYTES = 16 * 1024
MAX_TELEMETRY_AGE = 90
MAX_TELEMETRY_GAP = 75
MAX_BURNER_CYCLE_SECONDS = 6 * 3600
MAX_FUEL_SUMMARY_AGE = 15 * 60


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing telemetry timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("telemetry timestamp must include timezone")
    stamp = parsed.timestamp()
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("invalid telemetry timestamp")
    return stamp


def _regular_file(path):
    flags = os.O_RDONLY | os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("model artifact must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("model artifact exceeds size limit")
        return raw
    finally:
        os.close(descriptor)


class LiveMLRuntime:
    """One poller writer, read-only HTTP readers, and fail-closed model loading."""

    def __init__(self, *, burner_path=None, fuel_path=None, db_path=None):
        self.burner_path = (burner_path or "").strip() or None
        self.fuel_path = (fuel_path or "").strip() or None
        configured_db = (db_path or os.getenv("THERMOFLOW_DB_PATH", "")).strip()
        # Do not create a missing DB or run migrations from the API runtime.
        self._store = SQLiteStore(configured_db) if configured_db and os.path.isfile(configured_db) else None
        self._burner_model_version = None
        self._prediction_entity_key = None
        self._prediction_model_version = None
        self._lock = Lock()
        self._burner = None
        self._fuel = None
        self._burner_state = "not_configured" if not self.burner_path else "not_loaded"
        self._fuel_state = "not_configured" if not self.fuel_path else "not_loaded"
        self._last_timestamp = None
        self._last_fire = None
        self._cycle_started = None
        self._last_off = None
        self._previous_duration = None
        self._prediction = None
        # Only a confirmed OFF -> ON transition in this process can own a live
        # cycle. A restart or telemetry gap deliberately leaves old rows pending.
        self._cycle_entity_key = None
        self._cycle_model_version = None

    @property
    def configured(self):
        return bool(self.burner_path or self.fuel_path or self._store)

    def _load(self, path, loader, now):
        if not path:
            return "not_configured", None
        try:
            return "loaded", loader(_regular_file(path), now=now)
        except FileNotFoundError:
            return "missing_artifact", None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return "invalid_or_stale_artifact", None

    def refresh(self, *, now=None):
        clock = time.time() if now is None else now
        burner_version = None
        if self._store is not None:
            try:
                stored = load_burner_model_artifact(self._store, now=clock)
                if stored is not None:
                    burner_state, burner = "loaded_sqlite", stored["artifact"]
                    burner_version = stored["model_version"]
                else:
                    burner_state, burner = self._load(self.burner_path, load_burner_artifact, clock)
            except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                # Corrupt/stale DB artifacts fail closed; do not fall back to an older file.
                burner_state, burner = "invalid_or_stale_artifact", None
        else:
            burner_state, burner = self._load(self.burner_path, load_burner_artifact, clock)
        fuel_state, fuel = self._load(self.fuel_path, load_fuel_artifact, clock)
        with self._lock:
            self._burner_state, self._burner = burner_state, burner
            self._burner_model_version = burner_version
            self._fuel_state, self._fuel = fuel_state, fuel

    def _persist_live_prediction(self, model_version, entity_key, stamp, prediction):
        """Insert one pending outcome for a SQLite-backed live prediction."""
        if self._store is None or not model_version or not entity_key:
            return False
        payload = {
            "value": prediction["prediction"], "unit": "minutes",
            "features": prediction["features"], "mode": "live_shadow_only",
            "source": "live_fire_transition",
            "artifact_created_at": prediction.get("artifact_created_at"),
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
            with self._store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT prediction_json,evaluation_status FROM shadow_predictions "
                    "WHERE model_name=? AND model_version=? AND entity_key=?",
                    ("burner_cycle_duration", model_version, entity_key),
                ).fetchone()
                if existing is not None:
                    return (existing["evaluation_status"] == "pending"
                            and existing["prediction_json"] == encoded)
                cursor = db.execute(
                    "INSERT INTO shadow_predictions(model_name,model_version,entity_key,"
                    "predicted_at,target_at,prediction_json,actual_json,evaluation_status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("burner_cycle_duration", model_version, entity_key, stamp, None,
                     encoded, None, "pending"),
                )
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            # Persistence is observational; a DB problem must not affect polling.
            return False

    def _persist_live_actual(self, model_version, entity_key, started_at, ended_at, duration):
        """Close only the exact pending prediction owned by this observed cycle."""
        if self._store is None or not model_version or not entity_key:
            return False
        actual = {
            "value": round(float(duration), 4), "unit": "minutes",
            "source": "live_fire_transition",
            "cycle_started_at": int(started_at), "cycle_ended_at": int(ended_at),
            "complete_observed_cycle": True,
        }
        try:
            encoded = json.dumps(actual, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
            with self._store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute(
                    "UPDATE shadow_predictions SET target_at=?,actual_json=?,"
                    "evaluation_status='evaluated' WHERE model_name=? AND model_version=? "
                    "AND entity_key=? AND evaluation_status='pending' AND actual_json IS NULL",
                    (int(ended_at), encoded, "burner_cycle_duration", model_version, entity_key),
                )
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            return False

    def observe(self, payload):
        """Consume one fresh poll; gaps and disconnected reads break cycle evidence."""
        try:
            if not isinstance(payload, dict) or payload.get("connected") is not True:
                raise ValueError("telemetry unavailable")
            stamp = _timestamp(payload.get("fetched_at"))
            fire = payload["data"].get("fire")
            if type(fire) not in (bool, int, float) or fire not in (0, 1):
                raise ValueError("FIRE state unavailable")
            fire = bool(fire)
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            with self._lock:
                self._last_timestamp = None
                self._last_fire = None
                self._cycle_started = None
                self._last_off = None
                self._previous_duration = None
                self._prediction = None
                self._cycle_entity_key = None
                self._cycle_model_version = None
            return

        with self._lock:
            if self._last_timestamp is not None and stamp <= self._last_timestamp:
                return
            if (self._last_timestamp is not None
                    and stamp - self._last_timestamp > MAX_TELEMETRY_GAP):
                self._last_fire = None
                self._cycle_started = None
                self._last_off = None
                self._previous_duration = None
                self._prediction = None
                # Never close a persisted cycle across a telemetry gap.
                self._cycle_entity_key = None
                self._cycle_model_version = None

            if self._last_fire is None:
                self._last_fire = fire
                if fire:
                    # A process start (or telemetry gap) during FIRE is not a
                    # confirmed cycle boundary; never learn from a partial cycle.
                    self._cycle_started = None
                else:
                    self._last_off = stamp
            elif fire != self._last_fire:
                if fire:
                    self._cycle_started = stamp
                    self._prediction = None
                    self._cycle_entity_key = None
                    self._cycle_model_version = None
                    if self._previous_duration is not None and self._last_off is not None:
                        idle = (stamp - self._last_off) / 60.0
                        if idle >= 0 and self._burner is not None:
                            try:
                                self._prediction = {
                                    **predict_cycle_duration(
                                        self._burner,
                                        previous_duration_minutes=self._previous_duration,
                                        idle_minutes=idle,
                                    ),
                                    "predicted_at": int(stamp),
                                    "cycle_started_at": int(stamp),
                                    "artifact_created_at": self._burner["created_at"],
                                }
                            except (ValueError, TypeError, KeyError, OverflowError):
                                self._prediction = None
                            else:
                                # Persist only predictions made by a registered
                                # SQLite model; file fallback remains read-only.
                                model_version = self._burner_model_version
                                if model_version and self._store is not None:
                                    entity_key = f"fire-cycle-live:{int(stamp)}"
                                    if self._persist_live_prediction(
                                        model_version, entity_key, int(stamp), self._prediction
                                    ):
                                        self._cycle_entity_key = entity_key
                                        self._cycle_model_version = model_version
                else:
                    if self._cycle_started is not None:
                        duration = (stamp - self._cycle_started) / 60.0
                        if 0 < duration <= MAX_BURNER_CYCLE_SECONDS / 60:
                            self._previous_duration = duration
                            if self._cycle_entity_key and self._cycle_model_version:
                                self._persist_live_actual(
                                    self._cycle_model_version, self._cycle_entity_key,
                                    int(self._cycle_started), int(stamp), duration,
                                )
                        else:
                            self._previous_duration = None
                    self._last_off = stamp
                    self._cycle_started = None
                    self._prediction = None
                    self._cycle_entity_key = None
                    self._cycle_model_version = None
                self._last_fire = fire

            self._last_timestamp = stamp

    def read(self, now, fuel_summary=None):
        with self._lock:
            burner_state, burner = self._burner_state, self._burner
            fuel_state, fuel = self._fuel_state, self._fuel
            last_timestamp, fire = self._last_timestamp, self._last_fire
            cycle_started, previous_duration = self._cycle_started, self._previous_duration
            prediction = dict(self._prediction) if self._prediction else None

        burner_result = {"status": "collecting", "reason": burner_state, "mode": "live_advisory"}
        if burner is not None:
            if last_timestamp is None or now - last_timestamp > MAX_TELEMETRY_AGE or now < last_timestamp:
                burner_result = {"status": "stale", "reason": "fire_telemetry_stale", "mode": "live_advisory"}
            elif fire is True:
                if cycle_started is None:
                    burner_result = {
                        "status": "collecting",
                        "reason": "waiting_for_confirmed_fire_transition",
                        "mode": "live_advisory",
                    }
                else:
                    elapsed_seconds = max(0.0, now - cycle_started)
                    elapsed_minutes = round(elapsed_seconds / 60.0, 1)
                    if elapsed_seconds > MAX_BURNER_CYCLE_SECONDS:
                        burner_result = {
                            "status": "collecting",
                            "reason": "cycle_exceeded_max_duration",
                            "cycle_active": True,
                            "elapsed_minutes": elapsed_minutes,
                            "mode": "live_advisory",
                        }
                    elif prediction is not None:
                        prediction["elapsed_minutes"] = elapsed_minutes
                        prediction["cycle_active"] = True
                        burner_result = {**prediction, "status": "predicted", "mode": "live_advisory"}
                    else:
                        burner_result = {
                            "status": "collecting",
                            "reason": "waiting_for_previous_complete_cycle_and_model",
                            "cycle_active": True,
                            "elapsed_minutes": elapsed_minutes,
                            "mode": "live_advisory",
                        }
            else:
                burner_result = {
                    "status": "collecting",
                    "reason": "waiting_for_next_fire_cycle",
                    "previous_cycle_minutes": round(previous_duration, 1) if previous_duration else None,
                    "mode": "live_advisory",
                }

        fuel_result = {"status": "collecting", "reason": fuel_state, "mode": "live_advisory"}
        if fuel is not None:
            try:
                if not isinstance(fuel_summary, dict) or fuel_summary.get("configured") is not True:
                    raise ValueError("fuel_summary_unavailable")
                fetched_at = _timestamp(fuel_summary.get("fetched_at"))
                if now < fetched_at or now - fetched_at > MAX_FUEL_SUMMARY_AGE:
                    raise ValueError("fuel_summary_stale")
                planning = fuel_summary.get("planning")
                today = planning.get("today") if isinstance(planning, dict) else None
                baseline = today.get("average_kg_day") if isinstance(today, dict) else None
                result = predict_fuel_rate(fuel, baseline_kg_per_day=baseline)
                fuel_result = {
                    **result,
                    "status": "predicted",
                    "evaluated_at": int(fetched_at),
                    "artifact_created_at": fuel["created_at"],
                    "source_as_of": fuel["source_as_of"],
                    "mode": "live_advisory",
                }
            except (ValueError, TypeError, KeyError, OverflowError):
                fuel_result = {
                    "status": "collecting",
                    "reason": "fuel_baseline_or_fresh_summary_unavailable",
                    "mode": "live_advisory",
                }
        return {"burner_ml": burner_result, "fuel_ml": fuel_result}
