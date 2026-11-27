"""Serve ThingSpeak ``feeds.json`` range reads from the local SQLite mirror.

The ThingSpeak collector already copies every telemetry entry into
``telemetry_samples``. Reading those rows locally replaces downloading the same
data again (a cold fuel summary pulls >10 MB of JSON from ThingSpeak) while
returning exactly the structure the existing code expects, so no model or
analysis code has to change.

The local answer is used only when it is provably equivalent; in every other
case ``try_serve`` returns ``None`` and the caller performs the original
ThingSpeak request. It never raises and never writes.

Falls back to ThingSpeak when:

* ``THERMOFLOW_READ_SOURCE=thingspeak`` (instant rollback from the stack env);
* the request is not a plain ``start``/``end``/``results`` range read (any
  server-side aggregation such as ``timescale`` or ``average`` is unsupported);
* the channel has no mirrored rows, or the window starts before the first
  mirrored sample and ThingSpeak has not been *proven* to hold nothing earlier.
  The proof is one small ThingSpeak request per channel (any entries before the
  first mirrored sample?), cached for the life of the process: an empty answer
  lets such windows be served locally (they simply start with the first mirrored
  row), while any entry, an error or an unreadable answer keeps using ThingSpeak.
  ``THERMOFLOW_LOCAL_FEEDS_START`` remains an optional operator override;
* the collector has not ingested up to (almost) the end of the window, so the
  mirror may be behind;
* more rows match than ``results`` allows (ThingSpeak's truncation rule is not
  guessed).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger("thermoflow.local_feeds")

DEFAULT_DB_PATH = "/var/lib/thermoflow/thermoflow.sqlite3"
SOURCE_PREFIX = "thingspeak:"
CURSOR_PREFIX = "thingspeak_history:"
SUPPORTED_PARAMS = frozenset({"start", "end", "timezone", "results", "api_key"})
MAX_RESULTS = 8000
FRESH_SECONDS = 15 * 60
_PATH = re.compile(r"/channels/(\d+)/feeds\.json$")
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()
_sources: dict[str, str] = {}       # channel id -> mirrored source name
_warned: set[tuple[str, str]] = set()
STATS: dict = {"local": 0, "fallback": {}}

PROBE_START = "2010-01-01 00:00:00"        # before ThingSpeak existed
PROBE_TIMEOUT_SECONDS = 15
PROBE_RETRY_DATA_SECONDS = 3600            # entries exist before the mirror: ask again in an hour
PROBE_RETRY_ERROR_SECONDS = 300            # probe failed or was unreadable: ask again in 5 minutes
_proofs: dict[str, tuple] = {}             # channel -> (verified-empty-before epoch | None, retry_at)
_channel_locks: dict[str, threading.Lock] = {}


def read_mode() -> str:
    value = os.getenv("THERMOFLOW_READ_SOURCE", "auto").strip().lower()
    return "thingspeak" if value in {"thingspeak", "remote", "off", "0", "false"} else "auto"


def _fallback(channel, reason):
    with _lock:
        STATS["fallback"][reason] = STATS["fallback"].get(reason, 0) + 1
        first = (str(channel), reason) not in _warned
        _warned.add((str(channel), reason))
    if first and reason not in {"disabled", "not_a_range_read"}:
        LOGGER.warning("Local feeds not used for channel %s (%s); using ThingSpeak", channel, reason)
    return None


def _bound(text, tz_name):
    """Epoch seconds of a ThingSpeak ``start``/``end`` value (UTC unless ``timezone`` given)."""
    parsed = datetime.strptime(str(text), _TIME_FORMAT)
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    return int(parsed.replace(tzinfo=tz).timestamp())


def _override_start():
    raw = os.getenv("THERMOFLOW_LOCAL_FEEDS_START", "").strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return int(raw)
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None          # an unreadable override is ignored (stays conservative)


def _source_for(db, channel_id):
    with _lock:
        known = _sources.get(channel_id)
    if known:
        return known
    low, high = f"{channel_id}:", f"{channel_id};"       # ';' sorts right after ':'
    for row in db.execute("SELECT DISTINCT source FROM telemetry_samples WHERE source LIKE ?",
                          (SOURCE_PREFIX + "%",)):
        hit = db.execute(
            "SELECT 1 FROM telemetry_samples WHERE source=? AND source_key>=? AND source_key<? LIMIT 1",
            (row[0], low, high)).fetchone()
        if hit:
            with _lock:
                _sources[channel_id] = row[0]
            return row[0]
    return None


def _feed(row):
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    payload.pop("entry_id", None)          # legacy rows stored the full feed
    payload.pop("created_at", None)
    entry_id = int(str(row["source_key"]).split(":", 1)[1])
    created = datetime.fromtimestamp(row["observed_at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"created_at": created, "entry_id": entry_id, **payload}


def _now():
    return time.time()


def _fetch_json(url, params):
    request = Request(f"{url}?{urlencode(params)}",
                      headers={"Accept": "application/json", "User-Agent": "defro-dashboard-local-feeds"})
    with urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _nothing_earlier(url, channel, floor, api_key, clock):
    """True only if ThingSpeak itself returned no entry earlier than ``floor`` (epoch seconds).

    One probe per channel; concurrent windows of the same channel wait for it instead of
    repeating it. A proof for ``floor`` also covers any lower floor (a larger mirror).
    Errors are logged by type only: the request URL and the read key never reach the log.
    """
    with _lock:
        guard = _channel_locks.setdefault(channel, threading.Lock())
    with guard:
        with _lock:
            verified, retry_at = _proofs.get(channel, (None, 0.0))
        if verified is not None and verified >= floor:
            return True
        if verified is None and clock < retry_at:
            return False
        params = {"start": PROBE_START, "end": datetime.fromtimestamp(floor - 1, timezone.utc).strftime(_TIME_FORMAT),
                  "results": 1}
        if api_key:
            params["api_key"] = api_key
        proven, retry = False, PROBE_RETRY_ERROR_SECONDS
        try:
            payload = _fetch_json(url, params)
            feeds = payload.get("feeds") if isinstance(payload, dict) else None
            if isinstance(feeds, list):
                if feeds:
                    retry = PROBE_RETRY_DATA_SECONDS
                else:
                    proven = True
        except Exception as error:
            LOGGER.warning("ThingSpeak probe for channel %s failed (%s)", channel, type(error).__name__)
        with _lock:
            _proofs[channel] = (floor if proven else None, clock + retry)
        return proven


def try_serve(url, params, *, db_path=None, now=None):
    """Return a ThingSpeak-shaped ``{"channel", "feeds"}`` dict, or ``None`` to use ThingSpeak."""
    channel = "?"
    try:
        if read_mode() != "auto":
            return _fallback(channel, "disabled")
        match = _PATH.search(str(url).split("?", 1)[0])
        if not match or not isinstance(params, dict):
            return _fallback(channel, "not_a_range_read")
        channel = match.group(1)
        if not SUPPORTED_PARAMS.issuperset(params) or not {"start", "end", "results"} <= set(params):
            return _fallback(channel, "not_a_range_read")
        results = int(params["results"])
        if not 1 <= results <= MAX_RESULTS:
            return _fallback(channel, "not_a_range_read")
        tz_name = params.get("timezone")
        start, end = _bound(params["start"], tz_name), _bound(params["end"], tz_name)
        if end <= start:
            return _fallback(channel, "not_a_range_read")

        path = Path(db_path or os.getenv("THERMOFLOW_DB_PATH", DEFAULT_DB_PATH))
        if not path.is_file():
            return _fallback(channel, "no_database")
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            source = _source_for(db, channel)
            if source is None:
                return _fallback(channel, "unknown_channel")
            floor = _override_start()
            if floor is None:
                floor = db.execute("SELECT MIN(observed_at) FROM telemetry_samples WHERE source=?",
                                   (source,)).fetchone()[0]
            if floor is None or (start < floor and not _nothing_earlier(
                    str(url).split("?", 1)[0], channel, floor, params.get("api_key"), _now())):
                return _fallback(channel, "before_local_history")
            cursor = db.execute("SELECT cursor_value FROM collector_state WHERE collector_name=?",
                                (CURSOR_PREFIX + source[len(SOURCE_PREFIX):],)).fetchone()
            clock = int(time.time()) if now is None else now
            needed = min(end, clock)
            if cursor is None or int(cursor[0]) < needed - FRESH_SECONDS:
                return _fallback(channel, "collector_behind")
            rows = db.execute(
                "SELECT observed_at,source_key,payload_json FROM telemetry_samples "
                "WHERE source=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at,id LIMIT ?",
                (source, start, end, results + 1)).fetchall()
            if len(rows) > results:
                return _fallback(channel, "truncated")
            feeds = [_feed(row) for row in rows]
        finally:
            db.close()
        with _lock:
            STATS["local"] += 1
        return {"channel": {"id": int(channel)}, "feeds": feeds}
    except (ValueError, KeyError, TypeError, IndexError, OSError, sqlite3.Error, ZoneInfoNotFoundError):
        return _fallback(channel, "error")
    except Exception:           # never let an optimisation break the original read path
        return _fallback(channel, "error")


def reset_state():
    """Clear caches and counters (tests, and after the database file is replaced)."""
    with _lock:
        _sources.clear()
        _warned.clear()
        _proofs.clear()
        _channel_locks.clear()
        STATS["local"] = 0
        STATS["fallback"] = {}
