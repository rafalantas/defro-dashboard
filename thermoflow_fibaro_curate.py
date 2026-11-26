"""Build a minimal, read-only Fibaro heating dataset candidate.

Dry-run is the default. --apply writes a deterministic candidate dataset to
feature_windows/datasets after a verified SQLite backup. It NEVER deletes raw
Fibaro events and NEVER contacts HC3 or sends device commands.

Historical head-to-zone membership is not inferred from a later snapshot:
head-level transitions remain unassigned to a zone and are explicitly marked
as historically unverified.
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

FEATURE_SET = "fibaro_heating_event_v2"
DATASET_NAME = "fibaro_heating_history_v2"
DATASET_SCHEMA = "thermoflow-fibaro-curated-dataset/2"
FEATURE_SCHEMA = "thermoflow-fibaro-heating-feature/2"

DEVICE_PROPERTIES = {
    "heatingThermostatSetpoint": ("setpoint", "number", "C", 5.0, 35.0),
    "heatRequired": ("heat_required", "boolean", None, None, None),
    "windowOpened": ("window_open", "boolean", None, None, None),
}


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


def _finite_number(value, minimum, maximum):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
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
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        zone_id = _positive_id(zone.get("id"))
        if zone_id is not None:
            zone_by_id[zone_id] = zone

    heads_by_id = {}
    for head in heads:
        if not isinstance(head, dict):
            continue
        device_id = _positive_id(head.get("id"))
        if device_id is not None:
            heads_by_id[device_id] = head

    return heads_by_id, zone_by_id


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


def _snapshot_for(timestamp, snapshots):
    prior = [item for item in snapshots if item[0] <= timestamp]
    if prior:
        selected = prior[-1]
        return selected[1], selected[2], "snapshot_at_or_before_event", selected[0]
    if snapshots:
        selected = min(snapshots, key=lambda item: (item[0] - timestamp, item[0]))
        return selected[1], selected[2], "current_snapshot_identity_only", selected[0]
    return {}, {}, "no_snapshot", None


def _make_feature(event, snapshots):
    if not isinstance(event, dict):
        return None, "invalid_event"

    event_id = _positive_id(event.get("id"))
    timestamp = event.get("timestamp")
    if (
        event_id is None
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp <= 0
    ):
        return None, "invalid_identity"

    data = event.get("data")
    if not isinstance(data, dict):
        return None, "invalid_data"

    head_index, zone_index, snapshot_basis, snapshot_at = _snapshot_for(
        timestamp, snapshots
    )
    event_type = event.get("type")

    if event_type == "DevicePropertyUpdatedEvent":
        property_name = data.get("property")
        definition = DEVICE_PROPERTIES.get(property_name)
        if definition is None:
            return None, "unrelated_device_property"

        device_id = _positive_id(data.get("id"))
        if device_id is None or device_id not in head_index:
            return None, "unknown_head_id"

        signal, kind, unit, minimum, maximum = definition
        old_raw = data.get("oldValue")
        new_raw = data.get("newValue")
        if kind == "boolean":
            if not isinstance(old_raw, bool) or not isinstance(new_raw, bool):
                return None, "invalid_boolean_transition"
            old_value, new_value = old_raw, new_raw
        else:
            old_value = _finite_number(old_raw, minimum, maximum)
            new_value = _finite_number(new_raw, minimum, maximum)
            if old_value is None or new_value is None:
                return None, "invalid_setpoint_transition"

        if old_value == new_value:
            return None, "unchanged_value"

        return {
            "source_event_id": event_id,
            "entity": "head",
            "device_id": device_id,
            "zone_id": None,
            "signal": signal,
            "old_value": old_value,
            "new_value": new_value,
            "unit": unit,
            "identity_basis": (
                "historical_snapshot_at_or_before_event"
                if snapshot_basis == "snapshot_at_or_before_event"
                else "current_snapshot_identity_only"
            ),
            "snapshot_at": snapshot_at,
            "historical_zone_mapping": "unverified",
        }, None

    if event_type == "ClimateZoneTemperatureChangedEvent":
        raw_zone_id = data.get("id")
        if raw_zone_id is None:
            raw_zone_id = data.get("zoneId")
        zone_id = _positive_id(raw_zone_id)
        if zone_id is None or zone_id not in zone_index:
            return None, "unknown_zone_id"

        old_value = _finite_number(data.get("oldValue"), 0.0, 40.0)
        new_value = _finite_number(data.get("newValue"), 0.0, 40.0)
        if old_value is None or new_value is None:
            return None, "invalid_zone_temperature"
        if old_value == new_value:
            return None, "unchanged_value"

        return {
            "source_event_id": event_id,
            "entity": "zone",
            "device_id": None,
            "zone_id": zone_id,
            "signal": "zone_temperature",
            "old_value": old_value,
            "new_value": new_value,
            "unit": "C",
            "identity_basis": "zone_id_from_event_payload",
            "snapshot_at": snapshot_at,
            "historical_zone_mapping": "event_native_zone_id",
        }, None

    return None, "unrelated_event_type"


def build_curated_rows(db):
    snapshots = _load_snapshots(db)
    if not snapshots:
        raise RuntimeError(
            "Brak poprawnego snapshotu Fibaro; nie można zweryfikować "
            "tożsamości urządzeń i stref."
        )

    rows = db.execute(
        """SELECT event_id,observed_at,event_type,payload_json
           FROM fibaro_events ORDER BY observed_at,event_id"""
    ).fetchall()
    grouped = {}
    skipped = Counter()
    signals = Counter()
    mapping = Counter()
    seen_transitions = set()

    for row in rows:
        try:
            event = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            skipped["invalid_json"] += 1
            continue

        if not isinstance(event, dict):
            skipped["invalid_event"] += 1
            continue

        payload_id = _positive_id(event.get("id"))
        payload_timestamp = event.get("timestamp")
        payload_type = event.get("type")
        if (
            payload_id != int(row["event_id"])
            or isinstance(payload_timestamp, bool)
            or not isinstance(payload_timestamp, int)
            or payload_timestamp != int(row["observed_at"])
            or payload_type != row["event_type"]
        ):
            skipped["row_payload_mismatch"] += 1
            continue

        transition, reason = _make_feature(event, snapshots)
        if transition is None:
            skipped[reason] += 1
            continue

        timestamp = int(row["observed_at"])
        signature = (
            timestamp,
            transition["entity"],
            transition["device_id"],
            transition["zone_id"],
            transition["signal"],
            transition["old_value"],
            transition["new_value"],
        )
        if signature in seen_transitions:
            skipped["duplicate_transition"] += 1
            continue
        seen_transitions.add(signature)

        grouped.setdefault(timestamp, []).append(transition)
        signals[transition["signal"]] += 1
        mapping[transition["identity_basis"]] += 1

    features = []
    for timestamp, transitions in sorted(grouped.items()):
        values = {
            "schema": FEATURE_SCHEMA,
            "source": "fibaro-hc3",
            "observed_at": timestamp,
            "historical_head_zone_mapping": "unverified",
            "transitions": sorted(
                transitions, key=lambda item: item["source_event_id"]
            ),
        }
        features.append({
            "feature_set": FEATURE_SET,
            "window_start": timestamp,
            "window_end": timestamp + 1,
            "values_json": json.dumps(
                values,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        })

    digest_input = "\n".join(
        item["values_json"] for item in features
    ).encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()
    timestamps = [item["window_start"] for item in features]
    snapshot_times = [item[0] for item in snapshots]
    report = {
        "raw_events": len(rows),
        "curated_features": sum(
            len(json.loads(item["values_json"])["transitions"])
            for item in features
        ),
        "feature_windows": len(features),
        "signal_counts": dict(sorted(signals.items())),
        "skipped_by_reason": dict(sorted(skipped.items())),
        "identity_basis_counts": dict(sorted(mapping.items())),
        "first_feature_at": min(timestamps) if timestamps else None,
        "last_feature_at": max(timestamps) if timestamps else None,
        "snapshots_used": len(snapshots),
        "snapshot_range": {
            "first": min(snapshot_times),
            "last": max(snapshot_times),
        },
        "historical_head_zone_mapping": "unverified",
        "raw_source_events_preserved": True,
        "content_sha256": digest,
    }
    dataset_version = f"v2-{digest[:16]}"
    for item in features:
        item["dataset_version"] = dataset_version
    return features, report


def curate_database(db_path=DEFAULT_DB_PATH, *, apply=False, backup_dir=None):
    store = SQLiteStore(db_path)
    with store.connection() as db:
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "fibaro_events", "fibaro_snapshots", "feature_windows", "datasets"
        }
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(
                "Brak wymaganych tabel SQLite: " + ", ".join(missing)
            )

        raw_count = int(
            db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0]
        )
        if raw_count == 0:
            return {
                "status": "already_empty",
                "raw_events": 0,
                "curated_features": 0,
                "raw_source_events_preserved": True,
            }
        features, report = build_curated_rows(db)

    if apply and not features:
        raise RuntimeError(
            "Nie znaleziono poprawnych cech grzewczych; "
            "surowe zdarzenia pozostają bez zmian."
        )

    result = {"status": "dry_run" if not apply else "pending", **report}
    if not apply:
        return result

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target_dir = Path(backup_dir or (Path(db_path).parent / "backups"))
    backup_path = target_dir / f"thermoflow-before-fibaro-dataset-{stamp}.sqlite3"
    backup = store.backup_to(backup_path)
    if not backup.get("ok"):
        raise RuntimeError("Backup SQLite nie przeszedł weryfikacji; przerwano.")

    dataset_version = f"v2-{report['content_sha256'][:16]}"
    manifest = {
        "schema": DATASET_SCHEMA,
        "source": "fibaro-hc3",
        "feature_set": FEATURE_SET,
        "report": report,
        "source_event_payloads_retained": True,
        "hc3_queried": False,
        "device_commands_sent": False,
        "historical_head_zone_mapping": "unverified",
        "training_eligibility": (
            "candidate_only; do_not_join_heads_to_historical_zones "
            "without independent temporal mapping evidence"
        ),
    }
    manifest_json = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current_raw = int(
            db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0]
        )
        if current_raw != report["raw_events"]:
            raise RuntimeError(
                "Liczba surowych zdarzeń zmieniła się po audycie; przerwano."
            )

        for feature in features:
            existing = db.execute(
                """SELECT values_json FROM feature_windows
                   WHERE feature_set=? AND window_start=? AND window_end=?
                     AND dataset_version=?""",
                (
                    feature["feature_set"],
                    feature["window_start"],
                    feature["window_end"],
                    feature["dataset_version"],
                ),
            ).fetchone()
            if (
                existing is not None
                and existing["values_json"] != feature["values_json"]
            ):
                raise RuntimeError(
                    "Konflikt istniejącego feature_window; niczego nie zapisano."
                )
            db.execute(
                """INSERT OR IGNORE INTO feature_windows(
                    feature_set,window_start,window_end,generated_at,
                    dataset_version,values_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    feature["feature_set"],
                    feature["window_start"],
                    feature["window_end"],
                    int(time.time()),
                    feature["dataset_version"],
                    feature["values_json"],
                ),
            )

        stored = int(db.execute(
            """SELECT COUNT(*) FROM feature_windows
               WHERE feature_set=? AND dataset_version=?""",
            (FEATURE_SET, dataset_version),
        ).fetchone()[0])
        if stored != len(features):
            raise RuntimeError(
                "Liczba zapisanych okien nie zgadza się z datasetem; przerwano."
            )

        existing_dataset = db.execute(
            "SELECT content_sha256 FROM datasets WHERE name=? AND version=?",
            (DATASET_NAME, dataset_version),
        ).fetchone()
        if (
            existing_dataset is not None
            and existing_dataset["content_sha256"] != report["content_sha256"]
        ):
            raise RuntimeError("Konflikt wersji datasetu; przerwano.")

        db.execute(
            """INSERT OR IGNORE INTO datasets(
                name,version,created_at,manifest_json,content_sha256,status
            ) VALUES(?,?,?,?,?,?)""",
            (
                DATASET_NAME,
                dataset_version,
                int(time.time()),
                manifest_json,
                report["content_sha256"],
                "candidate",
            ),
        )

        for feature in features:
            stored_row = db.execute(
                """SELECT values_json FROM feature_windows
                   WHERE feature_set=? AND window_start=? AND window_end=?
                     AND dataset_version=?""",
                (
                    feature["feature_set"],
                    feature["window_start"],
                    feature["window_end"],
                    feature["dataset_version"],
                ),
            ).fetchone()
            if (
                stored_row is None
                or stored_row["values_json"] != feature["values_json"]
            ):
                raise RuntimeError(
                    "Weryfikacja cech nie powiodła się; transakcja wycofana."
                )

        remaining = int(
            db.execute("SELECT COUNT(*) FROM fibaro_events").fetchone()[0]
        )
        if remaining != report["raw_events"]:
            raise RuntimeError(
                "Surowe zdarzenia zmieniły się podczas zapisu; transakcja wycofana."
            )

        integrity = [
            str(row[0]) for row in db.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"]:
            raise RuntimeError(
                "SQLite integrity_check nie przeszedł; transakcja wycofana."
            )

    result.update({
        "status": "applied",
        "backup": backup["path"],
        "backup_integrity": backup["integrity_check"],
        "raw_events_deleted": 0,
        "fibaro_events_remaining": report["raw_events"],
        "raw_source_events_preserved": True,
        "fibaro_snapshots_preserved": True,
        "sqlite_integrity": "ok",
        "dataset_version": dataset_version,
        "dataset_status": "candidate",
    })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir")
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "create a verified backup and persist the candidate dataset; "
            "raw Fibaro events are retained"
        ),
    )
    args = parser.parse_args()
    result = curate_database(
        args.db, apply=args.apply, backup_dir=args.backup_dir
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
