#!/usr/bin/env python3
"""Create consistent SQLite snapshots and keep an encrypted Restic copy in R2."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_DB = Path(os.getenv("SOURCE_DB", "/source/thermoflow.sqlite3"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/Warsaw"))
LOCAL_RETENTION_HOURS = int(os.getenv("LOCAL_RETENTION_HOURS", "48"))
OFFSITE_RETENTION_DAYS = int(os.getenv("OFFSITE_RETENTION_DAYS", "90"))
OFFSITE_HOUR = int(os.getenv("OFFSITE_HOUR", "2"))
OFFSITE_MINUTE = int(os.getenv("OFFSITE_MINUTE", "30"))
RESTIC_REPOSITORY = os.environ["RESTIC_REPOSITORY"]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("thermoflow-backup")


def now() -> datetime:
    return datetime.now(TZ)


def make_snapshot() -> Path:
    if not SOURCE_DB.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {SOURCE_DB}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"thermoflow-hourly-{stamp}.sqlite3"
    temporary = target.with_suffix(".sqlite3.tmp")

    # Open source read-only. SQLite's backup API reads a transactionally
    # consistent view and includes committed WAL content.
    source_uri = SOURCE_DB.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(temporary)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.1)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {result}")
        destination.commit()
    finally:
        destination.close()
        source.close()

    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    log.info("SQLite snapshot created and verified: %s (%d bytes)", target, target.stat().st_size)
    return target


def restic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = RESTIC_REPOSITORY
    result = subprocess.run(
        ["restic", *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.stdout:
        log.info("restic: %s", result.stdout.strip())
    if check and result.returncode:
        raise RuntimeError(f"restic command failed ({result.returncode}): {' '.join(args)}")
    return result


def ensure_repository() -> None:
    probe = restic("snapshots", "--json", check=False)
    if probe.returncode:
        log.info("Restic repository not initialized (or unavailable); attempting init")
        restic("init")


def upload_offsite(snapshot: Path) -> None:
    ensure_repository()
    restic("backup", "--tag", "thermoflow-sqlite", "--host", "thermoflow-backup", str(snapshot))
    restic(
        "forget",
        "--tag", "thermoflow-sqlite",
        "--keep-daily", str(OFFSITE_RETENTION_DAYS),
        "--prune",
    )
    log.info("Offsite backup completed: %s", snapshot.name)


def cleanup_local() -> None:
    cutoff = now().timestamp() - LOCAL_RETENTION_HOURS * 3600
    removed = 0
    for path in BACKUP_DIR.glob("thermoflow-hourly-*.sqlite3"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            pass
    if removed:
        log.info("Local retention removed %d expired snapshot(s)", removed)


def next_hourly(after: datetime) -> datetime:
    candidate = after.replace(minute=5, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(hours=1)
    return candidate


def next_offsite(after: datetime) -> datetime:
    candidate = after.replace(
        hour=OFFSITE_HOUR, minute=OFFSITE_MINUTE, second=0, microsecond=0
    )
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def main() -> None:
    log.info("ThermoFLOW backup service starting")
    log.info("Source=%s local=%s repository=%s", SOURCE_DB, BACKUP_DIR, RESTIC_REPOSITORY)

    latest_snapshot = make_snapshot()
    try:
        upload_offsite(latest_snapshot)
        offsite_due = next_offsite(now())
    except Exception:
        log.exception("Initial offsite upload failed; retrying in 15 minutes")
        offsite_due = now() + timedelta(minutes=15)

    hourly_due = next_hourly(now())
    while True:
        current = now()
        if current >= hourly_due:
            try:
                latest_snapshot = make_snapshot()
                cleanup_local()
                hourly_due = next_hourly(now())
            except Exception:
                log.exception("Hourly SQLite snapshot failed; retrying in 5 minutes")
                hourly_due = now() + timedelta(minutes=5)

        if current >= offsite_due:
            try:
                if not latest_snapshot.exists():
                    latest_snapshot = make_snapshot()
                upload_offsite(latest_snapshot)
                offsite_due = next_offsite(now())
            except Exception:
                log.exception("Offsite upload failed; retrying in 15 minutes")
                offsite_due = now() + timedelta(minutes=15)

        time.sleep(20)


if __name__ == "__main__":
    main()
