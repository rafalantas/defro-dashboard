"""Explicit operator CLI for the shared ThermoFlow SQLite database.

No command runs implicitly at API import or container startup. Migration is
available only through the explicit `migrate` subcommand. Status and check
are read-only; backup refuses to overwrite an existing destination.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from thermoflow_store import SQLiteStore

SCHEMA = "thermoflow-sqlite-admin/1"


def inspect_database(database: str | Path) -> dict:
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    store = SQLiteStore(path)
    with store.connection() as db:
        tables = {
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = 0
        applied = []
        if "schema_migrations" in tables:
            rows = db.execute(
                "SELECT version,name,checksum_sha256,applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
            applied = [dict(row) for row in rows]
            version = max((int(row["version"]) for row in rows), default=0)
        journal = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        integrity = [str(row[0]) for row in db.execute("PRAGMA integrity_check")]
    return {
        "schema": SCHEMA,
        "database": str(path),
        "exists": True,
        "size_bytes": path.stat().st_size,
        "journal_mode": journal,
        "schema_version": version,
        "migrations": applied,
        "integrity_check": integrity,
        "ok": integrity == ["ok"],
        "read_only": True,
    }


def create_backup(database: str | Path, destination: str | Path) -> dict:
    source = Path(database).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"database does not exist: {source}")
    if source == target:
        raise ValueError("backup destination must differ from source database")
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {target}")
    result = SQLiteStore(source).backup_to(target)
    if not result.get("ok") or result.get("integrity_check") != ["ok"]:
        raise sqlite3.DatabaseError("backup failed integrity verification")
    return {
        "schema": SCHEMA,
        "status": "backup_created",
        "source": str(source),
        "backup": str(target),
        "integrity_check": result["integrity_check"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to the ThermoFlow SQLite database")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Explicitly apply pending SQL migrations")
    commands.add_parser("status", help="Show schema, journal and integrity status (read-only)")
    commands.add_parser("check", help="Run SQLite integrity_check (read-only)")
    backup = commands.add_parser("backup", help="Create a new verified online backup")
    backup.add_argument("destination", help="New backup path; existing files are never overwritten")
    args = parser.parse_args(argv)

    store = SQLiteStore(args.database)
    if args.command == "migrate":
        result = store.migrate()
    elif args.command in {"status", "check"}:
        result = inspect_database(args.database)
        if args.command == "check":
            result = {
                "schema": SCHEMA,
                "database": result["database"],
                "integrity_check": result["integrity_check"],
                "ok": result["ok"],
                "read_only": True,
            }
    else:
        result = create_backup(args.database, args.destination)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
