"""Explicit one-shot Fibaro HC3 collection into the shared ThermoFlow store.

No scheduler or background worker is started. All upstream calls are GET-only.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from fibaro_hc3_api import build_hc3_snapshot
from fibaro_hc3_provider import FibaroHC3Provider, HC3Config
from thermoflow_fibaro_history_collector import (FibaroHistoryCollector, COLLECTOR_NAME, BACKFILL_COLLECTOR_NAME)
from thermoflow_store import SQLiteStore

MAX_COLLECTION_SECONDS = 7 * 86400


def collect_fibaro_once(provider, collector, *, start, end, collected_at=None,
                        capture_snapshot=True, collector_name=COLLECTOR_NAME):
    """Fetch one bounded event window and one current snapshot, then report coverage."""
    if type(start) is not int or type(end) is not int or start <= 0 or end <= start:
        raise ValueError("start and end must be positive ordered Unix timestamps")
    if end - start > MAX_COLLECTION_SECONDS:
        raise ValueError("collection window must not exceed seven days")
    stamp = int(time.time()) if collected_at is None else collected_at
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("collected_at must be a positive Unix timestamp")
    if capture_snapshot:
        snapshot = build_hc3_snapshot(provider)
        snapshot_result = collector.save_snapshot(snapshot, captured_at=stamp)
    else:
        snapshot = {"counts": {}}
        snapshot_result = {"status": "not_requested", "id": None, "last_observed_at": None}
    events = provider.get_events_range(start, end)
    batch = collector.save_batch(
        events, start=start, end=end, collected_at=stamp,
        collector_name=collector_name,
    )
    coverage = collector.event_coverage(start=start, end=end)
    complete = batch["status"] == "complete" and batch["invalid"] == 0
    return {
        "schema": "thermoflow-fibaro-collection/1",
        "status": "complete" if complete else "partial",
        "window": {"start": start, "end": end, "duration_seconds": end - start},
        "snapshot": {
            "status": snapshot_result["status"], "id": snapshot_result["id"],
            "captured_at": snapshot_result["last_observed_at"],
            "zones": snapshot.get("counts", {}).get("zones"),
            "heads": snapshot.get("counts", {}).get("heads"),
        },
        "events": batch,
        "coverage": {
            "status": "fetch_complete" if complete else "partial",
            **coverage,
            "stored_events_in_window": coverage["stored_events"],
            "observed_span_seconds": (
                coverage["last_event_timestamp"] - coverage["first_event_timestamp"]
                if coverage["first_event_timestamp"] is not None
                and coverage["last_event_timestamp"] is not None else 0
            ),
            "no_events_observed": coverage["stored_events"] == 0,
            "interpretation": (
                "HC3 event history is event-driven; an empty interval is not proof of a data gap, "
                "and no regular sampling cadence is inferred."
            ),
        },
        "safety": {
            "read_only_upstream": True, "http_method": "GET",
            "background_worker_started": False, "device_commands_sent": False,
            "heating_settings_changed": False, "production_forecast_changed": False,
        },
    }


def collect_fibaro_incremental(provider, collector, *, now, initial_start=None,
                                overlap_seconds=120, collected_at=None,
                                capture_snapshot=True):
    """Resume from the last successful cursor, re-fetching a bounded overlap."""
    if type(now) is not int or now <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    if type(overlap_seconds) is not int or not 0 <= overlap_seconds <= 3600:
        raise ValueError("overlap_seconds must be between 0 and 3600")
    cursor = collector.status().get("last_successful_end")
    if cursor is None:
        if type(initial_start) is not int or initial_start <= 0:
            raise ValueError("initial_start is required before the first successful collection")
        start = initial_start
    else:
        start = max(1, int(cursor) - overlap_seconds)
    if now <= start:
        return {"schema": "thermoflow-fibaro-collection/1", "status": "idle",
                "reason": "no_new_time_range", "cursor_before": cursor,
                "safety": {"read_only_upstream": True, "device_commands_sent": False}}
    end = min(now, start + MAX_COLLECTION_SECONDS)
    result = collect_fibaro_once(
        provider, collector, start=start, end=end, collected_at=collected_at,
        capture_snapshot=capture_snapshot, collector_name=COLLECTOR_NAME,
    )
    result["mode"] = "incremental"
    result["cursor_before"] = cursor
    result["cursor_after"] = collector.status().get("last_successful_end")
    result["catch_up_pending"] = end < now
    return result



def collect_fibaro_backfill(provider, collector, *, now, initial_start,
                            collected_at=None):
    """Complete the one-time requested history range before live incremental polling.

    Backfill has its own cursor so an earlier partial import cannot make the
    collector skip missing history before the existing live cursor.
    """
    if type(now) is not int or now <= 0:
        raise ValueError("now must be a positive Unix timestamp")
    if type(initial_start) is not int or initial_start <= 0 or initial_start >= now:
        raise ValueError("initial_start must be positive and earlier than now")
    state = collector.status(BACKFILL_COLLECTOR_NAME)
    if state.get("backfill_complete"):
        return {
            "schema": "thermoflow-fibaro-collection/1",
            "status": "already_complete",
            "mode": "backfill",
            "cursor_after": state.get("last_successful_end"),
            "safety": {"read_only_upstream": True, "device_commands_sent": False},
        }

    cursor = state.get("last_successful_end")
    start = max(initial_start, int(cursor)) if cursor is not None else initial_start
    chunks = []
    first_chunk = True
    while start < now:
        end = min(now, start + MAX_COLLECTION_SECONDS)
        result = collect_fibaro_once(
            provider, collector, start=start, end=end,
            collected_at=collected_at, capture_snapshot=first_chunk,
            collector_name=BACKFILL_COLLECTOR_NAME,
        )
        chunks.append(result)
        if result["status"] != "complete":
            return {
                "schema": "thermoflow-fibaro-collection/1",
                "status": "partial",
                "mode": "backfill",
                "chunks": chunks,
                "chunks_completed": sum(1 for item in chunks if item["status"] == "complete"),
                "cursor_after": collector.status(BACKFILL_COLLECTOR_NAME).get("last_successful_end"),
                "backfill_pending": True,
                "safety": {"read_only_upstream": True, "device_commands_sent": False},
            }
        cursor = collector.status(BACKFILL_COLLECTOR_NAME).get("last_successful_end")
        if cursor is None or cursor <= start:
            raise RuntimeError("Fibaro backfill cursor did not advance")
        start = int(cursor)
        first_chunk = False

    collector.mark_backfill_complete(now, recorded_at=collected_at, initial_start=initial_start)
    collector.mark_cursor(
        now, collector_name=COLLECTOR_NAME, recorded_at=collected_at,
        metadata={"source": "completed_fibaro_backfill",
                  "backfill_start": initial_start,
                  "backfill_completed_at": collected_at or int(time.time()),
                  "consecutive_failures": 0},
    )
    totals = {
        key: sum(int(item.get("events", {}).get(key, 0) or 0)
                 for item in chunks)
        for key in ("fetched", "inserted", "duplicates", "invalid", "conflicts")
    }
    return {
        "schema": "thermoflow-fibaro-collection/1",
        "status": "complete",
        "mode": "backfill",
        "window": {"start": initial_start, "end": now, "duration_seconds": now - initial_start},
        "chunks_completed": len(chunks),
        "events": totals,
        "cursor_after": collector.status(BACKFILL_COLLECTOR_NAME).get("last_successful_end"),
        "live_cursor_after": collector.status(COLLECTOR_NAME).get("last_successful_end"),
        "backfill_pending": False,
        "snapshot": next((item["snapshot"] for item in chunks
                          if item.get("snapshot", {}).get("status") != "not_requested"), None),
        "safety": {"read_only_upstream": True, "device_commands_sent": False,
                   "heating_settings_changed": False},
    }


def main(argv=None):
    """Run one explicit collection cycle from an already initialized database."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to the existing shared ThermoFlow SQLite database")
    parser.add_argument("--start", type=int, help="Inclusive UTC Unix timestamp for manual mode")
    parser.add_argument("--end", type=int, help="Inclusive UTC Unix timestamp for manual mode")
    parser.add_argument("--collected-at", type=int, help="Optional deterministic collection timestamp")
    parser.add_argument("--incremental", action="store_true", help="Resume from the last successful SQLite cursor")
    parser.add_argument("--initial-start", type=int, help="Required initial history start before the first successful run")
    parser.add_argument("--overlap-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if not Path(args.database).expanduser().is_file():
        parser.error("database file does not exist; run the explicit migration/setup step first")
    try:
        provider = FibaroHC3Provider(HC3Config.from_env())
        collector = FibaroHistoryCollector(SQLiteStore(args.database))
        if args.incremental:
            if args.start is not None or args.end is not None:
                parser.error("--start/--end cannot be combined with --incremental")
            result = collect_fibaro_incremental(
                provider, collector, now=int(time.time()),
                initial_start=args.initial_start,
                overlap_seconds=args.overlap_seconds,
                collected_at=args.collected_at,
            )
        else:
            if args.start is None or args.end is None:
                parser.error("manual mode requires both --start and --end")
            result = collect_fibaro_once(
                provider, collector, start=args.start, end=args.end,
                collected_at=args.collected_at,
            )
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] in {"complete", "idle"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
