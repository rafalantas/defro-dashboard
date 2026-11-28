#!/usr/bin/env python3
"""Consistent SQLite snapshots and encrypted, verified Restic backups to Cloudflare R2."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCE_DB = Path(os.getenv("SOURCE_DB", "/source/thermoflow.sqlite3"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
TIMEZONE = os.getenv("TZ", "Europe/Warsaw")
LOCAL_RETENTION_HOURS = int(os.getenv("LOCAL_RETENTION_HOURS", "48"))
OFFSITE_RETENTION_DAYS = int(os.getenv("OFFSITE_RETENTION_DAYS", "90"))
OFFSITE_HOUR = int(os.getenv("OFFSITE_HOUR", "2"))
OFFSITE_MINUTE = int(os.getenv("OFFSITE_MINUTE", "30"))
RESTIC_REPOSITORY = os.environ.get("RESTIC_REPOSITORY", "")
RESTIC_TIMEOUT_SECONDS = int(os.getenv("RESTIC_TIMEOUT_SECONDS", "3600"))
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
TZ = ZoneInfo(TIMEZONE)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("thermoflow-backup")


def validate_config() -> None:
    if LOCAL_RETENTION_HOURS < 1:
        raise ValueError("LOCAL_RETENTION_HOURS must be >= 1")
    if OFFSITE_RETENTION_DAYS < 1:
        raise ValueError("OFFSITE_RETENTION_DAYS must be >= 1")
    if not 0 <= OFFSITE_HOUR <= 23 or not 0 <= OFFSITE_MINUTE <= 59:
        raise ValueError("OFFSITE_HOUR/MINUTE is outside the valid clock range")
    if RESTIC_TIMEOUT_SECONDS < 60:
        raise ValueError("RESTIC_TIMEOUT_SECONDS must be >= 60")
    if not RESTIC_REPOSITORY:
        raise ValueError("RESTIC_REPOSITORY is required")
    if not os.getenv("RESTIC_PASSWORD"):
        raise ValueError("RESTIC_PASSWORD is required")
    if not os.getenv("AWS_ACCESS_KEY_ID") or not os.getenv("AWS_SECRET_ACCESS_KEY"):
        raise ValueError("R2 credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) are required")


def now() -> datetime:
    return datetime.now(TZ)


def alert(message: str) -> None:
    """Send a best-effort Discord-compatible webhook notification."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"content": f"ThermoFLOW backup: {message[:1500]}"}).encode()
        request = Request(
            ALERT_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise RuntimeError(f"webhook returned HTTP {response.status}")
    except Exception:
        log.exception("Could not send backup alert webhook")


def make_snapshot() -> Path:
    if not SOURCE_DB.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {SOURCE_DB}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUP_DIR / f"thermoflow-hourly-{stamp}.sqlite3"
    temporary = target.with_name(target.name + ".tmp")
    source = None
    destination = None
    try:
        # URI mode=ro prevents writes to the live database. SQLite's backup API
        # reads a consistent view, including committed transactions in WAL.
        source_uri = SOURCE_DB.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        destination = sqlite3.connect(temporary)
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.1)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {result!r}")
        destination.commit()
        destination.close()
        destination = None
        source.close()
        source = None
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        log.info("Verified SQLite snapshot: %s (%d bytes)", target.name, target.stat().st_size)
        return target
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        temporary.unlink(missing_ok=True)


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
        timeout=RESTIC_TIMEOUT_SECONDS,
    )
    if result.stdout:
        # Never dump the environment or credentials into logs.
        log.info("restic: %s", result.stdout.strip())
    if check and result.returncode:
        raise RuntimeError(f"restic command failed ({result.returncode}): {' '.join(args)}")
    return result


def ensure_repository() -> None:
    probe = restic("snapshots", "--json", check=False)
    if probe.returncode == 0:
        return
    # Restic exit code 10 means the repository does not exist. Authentication,
    # network, lock and password errors must not be masked by attempting init.
    if probe.returncode != 10:
        raise RuntimeError(
            f"Cannot access Restic repository (exit {probe.returncode}); refusing to initialize it"
        )
    log.info("Restic repository does not exist; initializing it once")
    restic("init")


def verify_remote_restore() -> None:
    """Restore the just-uploaded latest snapshot and run SQLite integrity_check."""
    with tempfile.TemporaryDirectory(prefix="thermoflow-restore-check-") as directory:
        restic("restore", "latest", "--tag", "thermoflow-sqlite", "--target", directory)
        restored = list(Path(directory).rglob("thermoflow-hourly-*.sqlite3"))
        if not restored:
            raise RuntimeError("Restic restore verification found no SQLite snapshot")
        # A tagged repo should contain one database file per snapshot. Check the
        # newest file restored by this operation; fail closed if it is corrupt.
        candidate = max(restored, key=lambda path: path.stat().st_mtime)
        connection = sqlite3.connect(f"file:{candidate.resolve().as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"Restored SQLite integrity_check failed: {result!r}")
        finally:
            connection.close()
        log.info("Remote restore verification passed: %s", candidate.name)


def upload_offsite(snapshot: Path) -> None:
    ensure_repository()
    restic(
        "backup", "--tag", "thermoflow-sqlite", "--host", "thermoflow-backup",
        "--", str(snapshot),
    )
    verify_remote_restore()
    restic(
        "forget", "--tag", "thermoflow-sqlite",
        "--keep-daily", str(OFFSITE_RETENTION_DAYS), "--prune",
    )
    log.info("Offsite backup and restore verification completed: %s", snapshot.name)


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


def run_guarded(label: str, operation) -> bool:
    try:
        operation()
        return True
    except Exception as exc:
        log.exception("%s failed", label)
        alert(f"{label} failed: {type(exc).__name__}: {exc}")
        return False


def main() -> None:
    validate_config()
    log.info("ThermoFLOW backup starting; source=%s local=%s", SOURCE_DB, BACKUP_DIR)

    latest_snapshot: Path | None = None

    def create_local() -> None:
        nonlocal latest_snapshot
        latest_snapshot = make_snapshot()
        cleanup_local()

    run_guarded("Initial local snapshot", create_local)
    if latest_snapshot is not None:
        if run_guarded("Initial R2 backup", lambda: upload_offsite(latest_snapshot)):
            offsite_due = next_offsite(now())
        else:
            offsite_due = now() + timedelta(minutes=15)
    else:
        offsite_due = now() + timedelta(minutes=5)

    hourly_due = next_hourly(now())
    while True:
        current = now()
        if current >= hourly_due:
            if run_guarded("Hourly SQLite snapshot", create_local):
                hourly_due = next_hourly(now())
            else:
                hourly_due = now() + timedelta(minutes=5)

        if current >= offsite_due:
            if latest_snapshot is None or not latest_snapshot.exists():
                run_guarded("Snapshot before R2 upload", create_local)
            if latest_snapshot is not None and latest_snapshot.exists():
                if run_guarded("Daily R2 backup", lambda: upload_offsite(latest_snapshot)):
                    offsite_due = next_offsite(now())
                else:
                    offsite_due = now() + timedelta(minutes=15)
            else:
                offsite_due = now() + timedelta(minutes=5)

        time.sleep(20)


if __name__ == "__main__":
    main()
