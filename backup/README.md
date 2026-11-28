# ThermoFLOW Backup

Independent Portainer stack for consistent SQLite snapshots and encrypted offsite backups to Cloudflare R2.

## Backup behavior

- Creates a transactionally consistent SQLite snapshot immediately after startup and then hourly (at minute 05).
- Uses SQLite Online Backup API against the live database opened with `mode=ro`; committed WAL transactions are included.
- Runs `PRAGMA integrity_check` before publishing each local snapshot.
- Keeps local hourly snapshots for 48 hours by default.
- Uploads one verified snapshot to the encrypted Restic repository in Cloudflare R2 daily at 02:30 Europe/Warsaw.
- After every upload, restores the remote snapshot into a temporary directory and runs SQLite `integrity_check`. Retention pruning happens only after that restore check succeeds.
- Keeps 90 daily Restic snapshots by default.
- R2 failures do not stop hourly local snapshots. Failed offsite operations are retried after 15 minutes.
- An optional Discord-compatible webhook receives failure notifications. Docker health becomes unhealthy if no local snapshot has been produced for two hours.

The service never modifies or deletes the production database. Do not mount the production DB directory read-only: SQLite may need access to existing WAL shared-memory files for read-only WAL operation. The database connection itself is explicitly opened in read-only mode.

## Deploy in Portainer

Create a separate stack named `thermoflow-backup`:

- Repository: `https://github.com/rafalantas/thermoflow`
- Branch: `main`
- Compose path: `backup/compose.yml`

GitHub Actions publishes `ghcr.io/rafalantas/thermoflow-backup:latest` for `linux/amd64` and `linux/arm64`. Backup image builds and tests run in a separate workflow and do not trigger the dashboard/API Portainer webhook.

Set these stack environment variables:

| Variable | Required | Default / purpose |
| --- | --- | --- |
| `R2_ACCOUNT_ID` | yes | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | yes | R2 S3 access key ID |
| `R2_SECRET_ACCESS_KEY` | yes | R2 S3 secret |
| `R2_BUCKET` | no | `thermoflow-backups` |
| `RESTIC_PASSWORD` | yes | Strong unique encryption password |
| `LOCAL_RETENTION_HOURS` | no | `48` |
| `OFFSITE_RETENTION_DAYS` | no | `90` |
| `OFFSITE_HOUR` | no | `2` |
| `OFFSITE_MINUTE` | no | `30` |
| `RESTIC_TIMEOUT_SECONDS` | no | `3600` |
| `ALERT_WEBHOOK_URL` | no | Discord-compatible webhook URL |
| `TZ` | no | `Europe/Warsaw` |

Create an R2 API token scoped only to the backup bucket, with object read/write permissions. The Restic password is separate from the R2 credentials: store it outside the Raspberry Pi as well. If it is lost, the encrypted backup cannot be decrypted.

Local snapshots are written to `/mnt/thermoflow/db/backups/automatic`. Ensure this directory has sufficient free space. The source bind mount is `/mnt/thermoflow/db`.

## Verify operation

In Portainer, check that the container health is `healthy`. Logs should show:

- `Verified SQLite snapshot`
- `Remote restore verification passed`
- `Offsite backup and restore verification completed`

A healthy container only confirms that recent local snapshots exist; it does not replace checking the daily remote-restore verification log. Configure `ALERT_WEBHOOK_URL` if you want immediate failure notifications.

## Manual restore procedure

Never restore over the running production database. Stop the ThermoFLOW writers only during a planned cutover, and first restore to a separate directory.

Install Restic on a trusted machine and provide the same four environment values used by the backup container:

```sh
export RESTIC_REPOSITORY="s3:https://ACCOUNT_ID.r2.cloudflarestorage.com/thermoflow-backups/thermoflow"
export AWS_ACCESS_KEY_ID="R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="auto"
export RESTIC_PASSWORD="the-original-restic-password"
restic snapshots --tag thermoflow-sqlite
mkdir -p /tmp/thermoflow-restore
restic restore latest --tag thermoflow-sqlite --target /tmp/thermoflow-restore
find /tmp/thermoflow-restore -type f -name 'thermoflow-hourly-*.sqlite3' -print
```

Restic preserves the backed-up path under the restore target. Use the exact path printed by `find` for an integrity check:

```sh
python3 -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True); print(c.execute("PRAGMA integrity_check").fetchone()[0]); c.close()' /tmp/thermoflow-restore/backups/thermoflow-hourly-YYYYMMDD-HHMMSS-ffffff.sqlite3
```

Proceed with a controlled cutover only when the check returns `ok`. Preserve the original database and existing backup files until the application is running correctly on the restored copy.
