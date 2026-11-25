"""Shared, versioned SQLite storage foundation for ThermoFlow.

This module is intentionally opt-in in this first step: importing it does not
open a database, run migrations, start collectors, or change API behaviour.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = "/var/lib/thermoflow/thermoflow.sqlite3"
_MIGRATION_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """Raised when the migration history is unsafe or inconsistent."""


class SQLiteStore:
    """Small connection-per-operation SQLite store with atomic SQL migrations."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        migrations_dir: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.path = str(path or os.getenv("THERMOFLOW_DB_PATH", DEFAULT_DB_PATH))
        self.migrations_dir = Path(
            migrations_dir or Path(__file__).resolve().parent / "db" / "migrations"
        )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and commit/rollback its transaction atomically."""
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _migration_files(self) -> list[tuple[int, Path]]:
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"migration directory does not exist: {self.migrations_dir}")
        found: list[tuple[int, Path]] = []
        seen: set[int] = set()
        for path in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if not match:
                raise MigrationError(f"invalid migration filename: {path.name}")
            version = int(match.group(1))
            if version in seen:
                raise MigrationError(f"duplicate migration version: {version}")
            seen.add(version)
            found.append((version, path))
        return sorted(found)

    @staticmethod
    def _statements(script: str) -> list[str]:
        statements: list[str] = []
        pending = ""
        for line in script.splitlines():
            pending += line + "\n"
            if sqlite3.complete_statement(pending):
                statement = pending.strip()
                if statement:
                    statements.append(statement)
                pending = ""
        if pending.strip():
            raise MigrationError("migration contains an incomplete SQL statement")
        return statements

    def migrate(self) -> dict:
        """Apply pending migrations; each file and its ledger row commit together."""
        db = self._connect()
        applied_now: list[int] = []
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    applied_at INTEGER NOT NULL
                )"""
            )
            db.commit()

            files = self._migration_files()
            known_versions = {version for version, _ in files}
            rows = db.execute(
                "SELECT version, name, checksum_sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
            applied = {int(row["version"]): row for row in rows}
            unknown = sorted(set(applied) - known_versions)
            if unknown:
                raise MigrationError(f"database contains unknown migration versions: {unknown}")

            for version, path in files:
                raw = path.read_bytes()
                checksum = hashlib.sha256(raw).hexdigest()
                previous = applied.get(version)
                if previous is not None:
                    if previous["name"] != path.name or previous["checksum_sha256"] != checksum:
                        raise MigrationError(f"applied migration was modified: {path.name}")
                    continue

                db.execute("BEGIN IMMEDIATE")
                try:
                    script = raw.decode("utf-8")
                    for statement in self._statements(script):
                        db.execute(statement)
                    db.execute(
                        "INSERT INTO schema_migrations(version,name,checksum_sha256,applied_at) "
                        "VALUES(?,?,?,?)",
                        (version, path.name, checksum, int(time.time())),
                    )
                    db.commit()
                    applied_now.append(version)
                except Exception:
                    db.rollback()
                    raise

            latest = max(known_versions, default=0)
            return {
                "ok": True,
                "applied": applied_now,
                "schema_version": latest,
                "pending": 0,
                "journal_mode": self.journal_mode(),
            }
        finally:
            db.close()

    def journal_mode(self) -> str:
        with self.connection() as db:
            row = db.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()

    def schema_version(self) -> int:
        with self.connection() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            row = db.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()
            return int(row[0])

    def integrity_check(self) -> dict:
        with self.connection() as db:
            rows = [str(row[0]) for row in db.execute("PRAGMA integrity_check").fetchall()]
        return {"ok": rows == ["ok"], "results": rows}

    def backup_to(self, destination: str | os.PathLike[str]) -> dict:
        """Create a transactionally consistent SQLite backup, including committed WAL data."""
        target_path = Path(destination).expanduser()
        if str(target_path) == self.path or (
            self.path != ":memory:" and target_path.resolve() == Path(self.path).expanduser().resolve()
        ):
            raise ValueError("backup destination must differ from the source database")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
            check = [str(row[0]) for row in target.execute("PRAGMA integrity_check").fetchall()]
            target.commit()
            if check != ["ok"]:
                raise sqlite3.DatabaseError(f"backup integrity check failed: {check}")
            return {"ok": True, "path": str(target_path), "integrity_check": check}
        finally:
            target.close()
            source.close()
