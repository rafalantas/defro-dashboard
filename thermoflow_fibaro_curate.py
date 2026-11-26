"""One-shot conversion of raw HC3 logs into curated heating features.

Default mode is a read-only dry run. Use --apply to back up the SQLite database,
write verified features to feature_windows/datasets, and only then delete raw
fibaro_events in the same transaction. No HC3 calls are made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path

from thermoflow_store import DEFAULT_DB_PATH, SQLiteStore

FEATURE_SET = "fibaro_heating_event_v1"
DATASET_NAME = "fibaro_heating_history"
DEVICE_PROPERTIES = {
    "heatingThermostatSetpoint": ("setpoint", "number", "C"),
    "heatRequired": ("heat_required", "boolean", None),
    "windowOpened": ("window_open", "boolean", None),
}
MAX_TEMPERATURE_C = 60.0


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0 or str(parsed) != str(value).strip():
        return None
    return parsed


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not -20.0 <= parsed <= MAX_TEMPERATURE_C:
        return None
    return parsed


def _snapshot_index(payload):
    if not isinstance(payload, dict):
        return {}, {}
    zones = payload.get("zones")
    heads = payload.get("heads")
    if not isinstance(zones, list) or not isinstance(heads, list):
        return {}, {}
    zone_by_id = {}
    device_zones = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = _positive_id(zone.get("id"))
        if zone_id is None:
            continue
        zone_by_id[zone_id] = zone
        props = zone.get("properties")
        device_ids = props.get("devices") if isinstance(props, dict) else []
        if isinstance(device_ids, list):
            for raw_id in device_ids:
                device_id = _positive_id(raw_id)
                if device_id is not None:
                    device_zones.setdefault(device_id, set()).add(zone_id)
    heads_by_id = {}
    for head in heads:
        if not isinstance(head, dict):
            continue
        device_id = _positive_id(head.get("id"))
        if device_id is not None:
            heads_by_id[device_id] = head
    return (
        {
            device_id: {
                "head": head,
                "zone_ids": sorted(device_zones.get(device_id, set())),
            }
            for device_id, head in heads_by_id.items()
        },
        zone_by_id,
    )


def _load_snapshots(db):
    rows = db.execute(
        "SELECT captured_at,payload_json FROM fibaro_snapshots ORDER BY captured_at,id"
    ).fetchall()
    snapshots = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        head_index, zone_index = _snapshot_index(payload)
        if head_index or zone_index:
            snapshots.append((int(row["captured_at"]), head_index, zone_index))
    return snapshots


def _mapping_for(timestamp, snapshots):
    prior = [item for item in snapshots if item[0] <= timestamp]
    if prior:
        selected = prior[-1]
        return selected[1], selected[2], "snapshot_at_or_before_event", selected[0]
    # The imported DB currently may contain only a current snapshot. We allow
    # a clearly marked fallback so useful transitions are not silently lost.
    # Device/zone IDs still have to match the live snapshot exactly.
    if snapshots:
        selected = snapshots[0]
        return selected[1], selected[2], "nearest_available_snapshot_after_event", selected[0]
    return {}, {}, "unmapped_no_snapshot", None


def _make_feature(event, snapshots):
    if not isinstance(event, dict):
        return None, "invalid_event"
    event_id = _positive_id(event.get("id"))
    timestamp = event.get("timestamp")
    if event_id is None or isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        return None, "invalid_identity"
    data = event.get("data")
    if not isinstance(data, dict):
        return None, "invalid_data"
    head_index, zone_index, mapping_basis, snapshot_at = _mapping_for(timestamp, snapshots)
    event_type = event.get("type")
    entity = None
    signal = None
    old_value = new_value = None
    unit = None
    device_id = zone_id = None
    device_name = zone_name = None

    if event_type == "DevicePropertyUpdatedEvent":
        device_id = _positive_id(data.get("id"))
        definition = DEVICE_PROPERTIES.get(data.get("property"))
        if device_id is None or device_id not in head_index:
            return None, "not_a_mapped_head"
        if len(head_index[device_id]["zone_ids"]) != 1:
            return None, "head_zone_mapping_missing_or_ambiguous"
        if definition is None:
            return None, "unrelated_head_property"
        signal, kind, unit = definition
        old_raw, new_raw = data.get("oldValue"), data.get("newValue")
        if kind == "boolean":
            if not isinstance(old_raw, bool) or not isinstance(new_raw, bool):
                return None, "invalid_boolean_transition"
            old_value, new_value = old_raw, new_raw
        else:
            old_value, new_value = _finite_number(old_raw), _finite_number(new_raw)
            if old_value is None or new_value is None:
                return None, "invalid_numeric_transition"
        zone_id = head_index[device_id]["zone_ids"][0]
        head = head_index[device_id]["head"]
        device_name = head.get("name") if isinstance(head.get("name"), str) else None
        zone = zone_index.get(zone_id, {})
        zone_name = zone.get("name") if isinstance(zone.get("name"), str) else None
        entity = "head"
    elif event_type == "ClimateZoneTemperatureChangedEvent":
        zone_id = _positive_id(data.get("id") or data.get("zoneId"))
        if zone_id is None or zone_id not in zone_index:
            return None, "zone_not_in_snapshot"
        old_value = _finite_number(data.get("oldValue"))
        new_value = _finite_number(data.get("newValue"))
        if old_value is None or new_value is None:
            return None, "invalid_zone_temperature"
        signal, unit, entity = "zone_temperature", "C", "zone"
        zone = zone_index[zone_id]
        zone_name = zone.get("name") if isinstance(zone.get("name"), str) else None
    else:
        return None, "unrelated_event_type"

    if old_value == new_value:
        return None, "unchanged_value"

    values = {
        "schema": "thermoflow-fibaro-heating-feature/1",
        "source": "fibaro-hc3",
        "source_event_id": event_id,
        "observed_at": timestamp,
        "entity": entity,
        "device_id": device_id,
        "device_name": device_name,
        "zone_id": zone_id,
        "zone_name": zone_name,
        "signal": signal,
        "old_value": old_value,
        "new_value": new_value,
        "unit": unit,
        "mapping_basis": mapping_basis,
        "mapping_snapshot_at": snapshot_at,
    }
    return {
        "feature_set": FEATURE_SET,
        "window_start": timestamp,
        "window_end": timestamp + 1,
        "dataset_version": f"hc3-event-{event_id}",
        "values_json": json.dumps(
            values, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ),
    }, None


def build_curated_rows(db):
    snapshots = _load_snapshots(db)
    if not snapshots:
        raise RuntimeError("Brak poprawnego snapshotu Fibaro; surowych zdarzeń nie można bezpiecznie mapować.")
    rows = db.execute(
        "SELECT event_id,observed_at,event_type,payload_json FROM fibaro_events ORDER BY observed_at,event_id"
    ).fetchall()
    features = []
    skipped = Counter()
    mapping = Counter()
    for row in rows:
        try:
            event = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            skipped["invalid_json"] += 1
            continue
        feature, reason = _make_feature(event, snapshots)
        if feature is None:
            skipped[reason] += 1
            continue
        features.append(feature)
        mapping[json.loads(feature["values_json"])["mapping_basis"]] += 1
    digest_input = "\n".join(item["values_json"] for item in features).encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()
    timestamps = [json.loads(item["values_json"])["observed_at"] for item in features]
    report = {
        "raw_events": len(rows),
        "curated_features": len(features),
        "skipped_by_reason": dict(sorted(skipped.items())),
        "mapping_basis_counts": dict(sorted(mapping.items())),
        "first_feature_at": min(timestamps) if timestamps else None,
        "last_feature_at": max(timestamps) if timestamps else None,
        "snapshots_used": len(snapshots),
        "content_sha256": digest,
    }
    return features, report


def curate_database(db_path=DEFAULT_DB_PATH, *, apply=False, backup_dir=None):
    store = SQLiteStore(db_path)
    with store.connection() as db:
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"fibaro_events", "fibaro_snapshots", "feature_windows", "datasets"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("Brak wymaganych tabel SQLite: " + ", ".join(missing))
        raw_count = int(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0])
        if raw_count == 0:
            return {"status": "already_empty", "raw_events": 0, "curated_features": 0}
        features, report = build_curated_rows(db)
    if apply and not features:
        raise RuntimeError(
            "Nie znaleziono żadnych zweryfikowanych cech grzewczych; "
            "surowe zdarzenia pozostają bez zmian."
        )

    result = {"status": "dry_run" if not apply else "pending", **report}
    if not apply:
        return result

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target_dir = Path(backup_dir or (Path(db_path).parent / "backups"))
    backup_path = target_dir / f"thermoflow-before-fibaro-curation-{stamp}.sqlite3"
    backup = store.backup_to(backup_path)
    if not backup.get("ok"):
        raise RuntimeError("Backup SQLite nie przeszedł weryfikacji; przerwano.")

    dataset_version = f"v1-{report['content_sha256'][:16]}"
    manifest = {
        "schema": "thermoflow-fibaro-curated-dataset/1",
        "source": "fibaro-hc3",
        "feature_set": FEATURE_SET,
        "report": report,
        "raw_event_payloads_deleted_after_verification": True,
        "hc3_queried": False,
        "device_commands_sent": False,
    }
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current_raw = int(db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0])
        if current_raw != report["raw_events"]:
            raise RuntimeError("Liczba surowych zdarzeń zmieniła się po audycie; przerwano.")
        for feature in features:
            existing = db.execute(
                """SELECT values_json FROM feature_windows
                   WHERE feature_set=? AND window_start=? AND window_end=? AND dataset_version=?""",
                (feature["feature_set"], feature["window_start"],
                 feature["window_end"], feature["dataset_version"]),
            ).fetchone()
            if existing is not None and existing["values_json"] != feature["values_json"]:
                raise RuntimeError("Konflikt istniejącego feature_window; niczego nie usunięto.")
            db.execute(
                """INSERT OR IGNORE INTO feature_windows(
                    feature_set,window_start,window_end,generated_at,dataset_version,values_json
                ) VALUES(?,?,?,?,?,?)""",
                (feature["feature_set"], feature["window_start"], feature["window_end"],
                 int(time.time()), feature["dataset_version"], feature["values_json"]),
            )

        stored = int(db.execute(
            "SELECT COUNT(*) FROM feature_windows WHERE feature_set=?",
            (FEATURE_SET,),
        ).fetchone()[0])
        expected_versions = {item["dataset_version"] for item in features}
        if stored < len(expected_versions):
            raise RuntimeError("Nie wszystkie cechy zostały zapisane; surowe logi zachowano.")

        existing_dataset = db.execute(
            "SELECT content_sha256,manifest_json FROM datasets WHERE name=? AND version=?",
            (DATASET_NAME, dataset_version),
        ).fetchone()
        if existing_dataset is not None and existing_dataset["content_sha256"] != report["content_sha256"]:
            raise RuntimeError("Konflikt wersji datasetu; surowe logi zachowano.")
        db.execute(
            """INSERT OR IGNORE INTO datasets(
                name,version,created_at,manifest_json,content_sha256,status
            ) VALUES(?,?,?,?,?,?)""",
            (DATASET_NAME, dataset_version, int(time.time()), manifest_json,
             report["content_sha256"], "candidate"),
        )

        # Re-read every inserted feature and verify the content before deleting
        # the source event payloads. The delete and inserts share this transaction.
        for feature in features:
            row = db.execute(
                """SELECT values_json FROM feature_windows
                   WHERE feature_set=? AND window_start=? AND window_end=? AND dataset_version=?""",
                (feature["feature_set"], feature["window_start"],
                 feature["window_end"], feature["dataset_version"]),
            ).fetchone()
            if row is None or row["values_json"] != feature["values_json"]:
                raise RuntimeError("Weryfikacja cech nie powiodła się; surowe logi zachowano.")

        deleted = db.execute("DELETE FROM fibaro_events").rowcount
        if deleted != report["raw_events"]:
            raise RuntimeError("Usunięto inną liczbę zdarzeń niż oczekiwano; transakcja wycofana.")
        integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise RuntimeError("SQLite integrity_check nie przeszedł; transakcja wycofana.")

    result.update({
        "status": "applied",
        "backup": backup["path"],
        "backup_integrity": backup["integrity_check"],
        "raw_events_deleted": deleted,
        "fibaro_events_remaining": 0,
        "fibaro_snapshots_preserved": True,
        "non_fibaro_tables_changed": False,
        "sqlite_integrity": "ok",
        "dataset_version": dataset_version,
    })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir")
    parser.add_argument("--apply", action="store_true",
                        help="create verified backup, persist curated features, then delete raw Fibaro events")
    args = parser.parse_args()
    result = curate_database(args.db, apply=args.apply, backup_dir=args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
