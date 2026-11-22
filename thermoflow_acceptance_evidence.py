"""Read-only loader for optional, operator-reviewed acceptance evidence files."""
import json
import os
import stat

from thermoflow_data_quality_gates import build_data_quality_report
from thermoflow_model_monitoring import monitor_models

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


def _read_json(path):
    if not path:
        return None
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence file must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence file exceeds size limit")
        return json.loads(raw)
    finally:
        os.close(descriptor)


def _configured(env, name):
    return (env.get(name) or "").strip()


def build_acceptance_evidence(*, now, environ=None):
    """Load only explicitly configured local snapshots, artifacts and ledgers.

    No upstream calls, writes, training, artifact promotion or controller I/O.
    Missing files stay not_assessed; malformed configured evidence is blocked.
    """
    env = os.environ if environ is None else environ
    quality = None
    quality_names = (
        "THERMOFLOW_FIRE_SNAPSHOT_PATH",
        "THERMOFLOW_FUEL_SNAPSHOT_PATH",
        "THERMOFLOW_VERIFIED_REFILL_IDS_PATH",
    )
    quality_configured = any(_configured(env, key) for key in quality_names)
    if quality_configured:
        try:
            fire = _read_json(_configured(env, quality_names[0]))
            fuel = _read_json(_configured(env, quality_names[1]))
            allowlist = _read_json(_configured(env, quality_names[2])) or {}
            ids = allowlist.get("verified_refill_ids", []) if isinstance(allowlist, dict) else []
            if not isinstance(ids, list):
                raise ValueError("invalid verified refill ID allowlist")
            quality = build_data_quality_report(fire=fire, fuel=fuel, verified_refill_ids=ids)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            quality = {"schema": "thermoflow-data-quality-report/1", "status": "blocked",
                       "modules": {}, "error": "configured_data_quality_evidence_invalid"}

    monitoring = None
    artifact_names = ("THERMOFLOW_BURNER_MODEL_PATH", "THERMOFLOW_FUEL_MODEL_PATH")
    ledger_names = ("THERMOFLOW_BURNER_LEDGER_PATH", "THERMOFLOW_FUEL_LEDGER_PATH")
    monitoring_configured = any(_configured(env, key) for key in artifact_names + ledger_names)
    if monitoring_configured:
        try:
            artifacts = {
                name: _read_json(_configured(env, key))
                for name, key in zip(("burner_ml", "fuel_ml"), artifact_names)
                if _configured(env, key)
            }
            ledgers = {
                name: _read_json(_configured(env, key))
                for name, key in zip(("burner_ml", "fuel_ml"), ledger_names)
                if _configured(env, key)
            }
            monitoring = monitor_models(now=now, artifacts=artifacts, ledgers=ledgers)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            monitoring = {"schema": "thermoflow-model-monitoring/1", "status": "review",
                          "modules": {}, "error": "configured_monitoring_evidence_invalid"}
    return {"data_quality": quality, "monitoring": monitoring}
