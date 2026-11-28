# ThermoFLOW Backup

Independent Portainer stack for consistent SQLite snapshots and encrypted offsite backups to Cloudflare R2.

## What it does

- Creates an initial SQLite snapshot on container start, then every hour at minute 05.
- Uses Python's SQLite Online Backup API; it does not copy the live database file with `cp`.
- Runs `PRAGMA integrity_check` on every snapshot before publishing it.
- Keeps hourly local snapshots for 48 hours.
- Uploads the latest verified snapshot to a Restic repository in Cloudflare R2 at 02:30 Europe/Warsaw.
- Retains 90 daily Restic snapshots. Restic encrypts and deduplicates repository data.

If the initial R2 upload fails, the service logs the error and retries after 15 minutes. It never deletes the source database.

## Portainer deployment

Create a separate stack named `thermoflow-backup` using this repository:

- Repository: `https://github.com/rafalantas/thermoflow`
- Branch: `main`
- Compose path: `backup/compose.yml`

The image is published by GitHub Actions as `ghcr.io/rafalantas/thermoflow-backup:latest` for amd64 and arm64.

Add these stack environment variables in Portainer:

| Variable | Value |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_BUCKET` | `thermoflow-backups` |
| `R2_ACCESS_KEY_ID` | R2 token Access Key ID |
| `R2_SECRET_ACCESS_KEY` | R2 token Secret Access Key |
| `RESTIC_PASSWORD` | A strong, unique password generated for this repository |
| `LOCAL_RETENTION_HOURS` | `48` |
| `OFFSITE_RETENTION_DAYS` | `90` |
| `OFFSITE_HOUR` | `2` |
| `OFFSITE_MINUTE` | `30` |
| `TZ` | `Europe/Warsaw` |

Do not put credentials in Git, a committed `.env` file, image build arguments, or logs. Store the Restic password separately from the Raspberry Pi; without it the encrypted repository cannot be restored.

## Storage

The source directory is mounted at `/source`. The backup process opens the SQLite database read-only and writes snapshots only under `/backups`, mapped to:

`/mnt/thermoflow/db/backups/automatic`

The source bind mount is writable at the filesystem level to allow SQLite read-only WAL access to its shared-memory sidecar when needed. The backup code does not execute writes against the source database.

## Restore

Do not restore over a running production database. First download a snapshot to a separate directory:

```sh
restic -r "$RESTIC_REPOSITORY" snapshots
restic -r "$RESTIC_REPOSITORY" restore latest --target /tmp/thermoflow-restore
```

Then run an integrity check against the restored SQLite file before planning a controlled restore:

```sh
python3 -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0]); c.close()' /tmp/thermoflow-restore/source/thermoflow-hourly-YYYYMMDD-HHMMSS.sqlite3
```

The exact restored path is shown by Restic's restore output. Keep the original database and all existing backups until the restored copy has been checked and the application has been deliberately switched.
