"""Opt-in five-minute Fibaro collection scheduler for a dedicated container.

This process is never started by the API. Enable the Compose profile
`fibaro-collector` explicitly after SQLite has been initialized and HC3
credentials plus an initial history timestamp have been configured.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from fibaro_hc3_provider import FibaroHC3Provider, HC3Config
from thermoflow_fibaro_collection import collect_fibaro_backfill, collect_fibaro_incremental
from thermoflow_fibaro_history_collector import FibaroHistoryCollector, COLLECTOR_NAME, BACKFILL_COLLECTOR_NAME
from thermoflow_store import SQLiteStore


def run_scheduler(cycle, *, interval_seconds=300, max_cycles=None, wait=time.sleep):
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 3600:
        raise ValueError("interval_seconds must be between 60 and 3600")
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    results = []
    while max_cycles is None or len(results) < max_cycles:
        try:
            result = cycle()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        except Exception as error:
            # Do not log exception text: upstream libraries can include URLs or
            # request details. Keep operational logs useful without leaking secrets.
            failure = {"status": "error", "error_type": type(error).__name__}
            results.append(failure)
            print(json.dumps(failure), flush=True)
        if max_cycles is None or len(results) < max_cycles:
            wait(interval_seconds)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Existing, migrated ThermoFlow SQLite database")
    parser.add_argument("--initial-start", type=int,
                        help="Optional fixed UTC Unix timestamp for the first collection window")
    parser.add_argument("--history-days", type=int, default=14,
                        help="One-time history range when no initial start is stored (default: 14 days)")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--overlap-seconds", type=int, default=120)
    parser.add_argument("--once", action="store_true", help="Run exactly one collection cycle")
    args = parser.parse_args(argv)
    if not Path(args.database).expanduser().is_file():
        parser.error("database file does not exist; run the explicit SQLite migration step first")
    if args.initial_start is not None and args.initial_start <= 0:
        parser.error("--initial-start must be a positive Unix timestamp")
    if not 1 <= args.history_days <= 14:
        parser.error("--history-days must be between 1 and 14")
    if not 0 <= args.overlap_seconds <= 3600:
        parser.error("--overlap-seconds must be between 0 and 3600")
    if not 60 <= args.interval_seconds <= 3600:
        parser.error("--interval-seconds must be between 60 and 3600")

    try:
        provider = FibaroHC3Provider(HC3Config.from_env())
        collector = FibaroHistoryCollector(SQLiteStore(args.database))
        def cycle():
            now = int(time.time())
            requested_start = args.initial_start or (now - args.history_days * 86400)
            stable_initial_start = collector.set_backfill_initial_start(
                requested_start, recorded_at=now
            )
            backfill_state = collector.status(BACKFILL_COLLECTOR_NAME)
            active_collector = (
                COLLECTOR_NAME if backfill_state.get("backfill_complete")
                else BACKFILL_COLLECTOR_NAME
            )
            try:
                if not backfill_state.get("backfill_complete"):
                    return collect_fibaro_backfill(
                        provider, collector, now=now,
                        initial_start=stable_initial_start,
                    )
                return collect_fibaro_incremental(
                    provider, collector, now=now,
                    initial_start=stable_initial_start,
                    overlap_seconds=args.overlap_seconds,
                )
            except Exception as error:
                cursor = collector.status(active_collector).get("last_successful_end")
                start = max(1, int(cursor or stable_initial_start) - args.overlap_seconds)
                collector.record_failure(
                    collector_name=active_collector,
                    error_type=type(error).__name__,
                    attempted_at=now,
                    window_start=start,
                    window_end=now,
                )
                raise

        run_scheduler(cycle, interval_seconds=args.interval_seconds,
                      max_cycles=1 if args.once else None)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
