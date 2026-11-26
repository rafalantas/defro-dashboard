# ThermoFlow SQLite on the Raspberry Pi SSD

## Storage contract

The API and the optional Fibaro collector must use the same host bind mount:

- Host directory: `/mnt/thermoflow/db`
- Container directory: `/var/lib/thermoflow`
- Database: `/var/lib/thermoflow/thermoflow.sqlite3`
- Backups: `/mnt/thermoflow/backups`
- The host mounts the ext4 image `/mnt/timemachine/thermoflow-storage.img` at `/mnt/thermoflow`.

The Compose bind mount uses `bind.create_host_path: false`. Do not replace it with short bind syntax: Docker must fail to create the container if the SSD-backed directory is missing, rather than silently creating a directory on the microSD root filesystem.

The old named Docker volume `defro-dashboard-thermoflow-db` is intentionally not declared or deleted by this change. Keep it until the SSD deployment has been verified and a separate retention decision is made.

## Cutover procedure (operator-run; do not merge before step 1)

The GitOps stack may redeploy as soon as this Compose change reaches its tracked branch. Seed the SSD database before merging the PR.

### 1. Verify the SSD mount and seed the database

Run on the Raspberry Pi while the current API is still using the old named volume:

```bash
set -e

mountpoint -q /mnt/thermoflow
findmnt -T /mnt/thermoflow
test -d /mnt/thermoflow/db
test ! -e /mnt/thermoflow/db/thermoflow.sqlite3

# Produce a consistent online SQLite backup from the live database.
docker exec defro-dashboard-api \
  python -m thermoflow_db_admin \
  /var/lib/thermoflow/thermoflow.sqlite3 backup \
  /tmp/thermoflow-pre-ssd.sqlite3

# Copy it into the SSD image before GitOps changes the container mount.
docker cp defro-dashboard-api:/tmp/thermoflow-pre-ssd.sqlite3 \
  /mnt/thermoflow/db/thermoflow.sqlite3
sudo chown root:root /mnt/thermoflow/db/thermoflow.sqlite3
sudo chmod 0644 /mnt/thermoflow/db/thermoflow.sqlite3

# Retain the separate pre-migration backup and clean the container temp file.
docker exec defro-dashboard-api rm /tmp/thermoflow-pre-ssd.sqlite3

ls -lh /mnt/thermoflow/db/thermoflow.sqlite3
```

Do not merge/deploy if the mountpoint check fails, the backup command fails, or the database file is not present on the SSD.

### 2. Deploy through Portainer GitOps

Merge the reviewed PR only after step 1 succeeds. Portainer should redeploy the stack from Git. Do not edit Portainer's generated Compose copy manually.

### 3. Verify the deployed mount and database

```bash
docker inspect defro-dashboard-api \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'

docker exec defro-dashboard-api \
  python -m thermoflow_db_admin \
  /var/lib/thermoflow/thermoflow.sqlite3 status

docker volume inspect defro-dashboard-thermoflow-db
```

Expected API mount source is `/mnt/thermoflow/db` and destination is `/var/lib/thermoflow`. The database status must report `exists: true`, `schema_version: 2`, and `integrity_check: ["ok"]`. The named volume should still exist as the untouched pre-cutover fallback.

If the Fibaro collector is enabled later, it receives the exact same bind mount in Compose.

## Boot and failure behaviour

The host `/etc/fstab` entry must mount the SSD image at `/mnt/thermoflow` after `/mnt/timemachine`. Keep the mount required (do not add `nofail`). The SSD image must be mounted before Docker starts. The long bind syntax with `create_host_path: false` is a second guard: a missing `/mnt/thermoflow/db` causes container creation to fail instead of creating a fallback directory on the microSD.

Verify the generated mount dependency after changing host mount configuration:

```bash
sudo systemctl daemon-reload
systemctl show mnt-thermoflow.mount -p Requires -p After
findmnt --verify --verbose
```

## Rollback

Do not delete `defro-dashboard-thermoflow-db`. If the SSD deployment must be rolled back after the API has written new data, first stop the API/collector and create a verified SQLite backup from the SSD database, then restore that current database into the named volume before reverting the Compose mount. Simply switching back to the old named volume would discard writes made after cutover.
