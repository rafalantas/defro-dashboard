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

The GitOps stack may redeploy as soon as this Compose change reaches its tracked branch. Seed and verify the SSD database before merging the PR.

### 1. Verify the SSD mount and seed the database

Run on the Raspberry Pi while the current API is still using the old named volume:

```bash
set -e

mountpoint -q /mnt/thermoflow
findmnt -T /mnt/thermoflow
test -d /mnt/thermoflow/db
test -d /mnt/thermoflow/backups

# Never overwrite an existing SSD database. If it is already present,
# keep it and verify it against the separately retained SQLite backup.
if [ -e /mnt/thermoflow/db/thermoflow.sqlite3 ]; then
  echo "SSD database already exists; refusing to overwrite it."
  ls -lah /mnt/thermoflow/db/thermoflow.sqlite3
  echo "Compare its SHA256 with the verified pre-cutover backup before proceeding."
  exit 1
fi

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

# Retain the verified backup outside the database directory.
docker cp defro-dashboard-api:/tmp/thermoflow-pre-ssd.sqlite3 \
  /mnt/thermoflow/backups/thermoflow-pre-ssd.sqlite3
docker exec defro-dashboard-api rm /tmp/thermoflow-pre-ssd.sqlite3

sha256sum /mnt/thermoflow/db/thermoflow.sqlite3 \
  /mnt/thermoflow/backups/thermoflow-pre-ssd.sqlite3
ls -lah /mnt/thermoflow/db/thermoflow.sqlite3
```

The two SHA256 values must match. If the database file already exists (as it does after a successful prior seed), do not rerun the copy sequence or overwrite it. Verify the existing file against the retained backup instead. Do not merge/deploy if the mountpoint check fails, the backup command fails, the hashes differ, or the database file is empty.

### 2. Install the Docker mount dependency

The Compose file protects against a missing bind source, but it cannot order the host's SSD mounts before the Docker daemon. Install the systemd drop-in included in this repository:

```bash
sudo install -D -m 0644 \
  deploy/systemd/docker.service.d/thermoflow-storage.conf \
  /etc/systemd/system/docker.service.d/thermoflow-storage.conf

sudo systemctl daemon-reload
systemctl show docker.service -p RequiresMountsFor -p Requires -p After
findmnt --verify --verbose
```

The drop-in requires both the backing Time Machine filesystem path and the ext4 image mount before Docker starts. The `/etc/fstab` entry for the image must mount it at `/mnt/thermoflow` after `/mnt/timemachine`. Keep these mounts required; do not add `nofail`. Do not restart Docker as part of this preparation step.

### 3. Deploy through Portainer GitOps

Merge the reviewed PR only after steps 1 and 2 succeed. Portainer should redeploy the stack from Git. Do not edit Portainer's generated Compose copy manually.

### 4. Verify the deployed mount and database

```bash
docker inspect defro-dashboard-api \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'

docker exec defro-dashboard-api \
  python -m thermoflow_db_admin \
  /var/lib/thermoflow/thermoflow.sqlite3 status

docker volume inspect defro-dashboard-thermoflow-db
```

Expected API mount source is `/mnt/thermoflow/db` and destination is `/var/lib/thermoflow`. The database status must report `exists: true`, `schema_version: 2`, and `integrity_check: ["ok"]`. Confirm the API can write by checking that normal telemetry continues to be recorded. The named volume should still exist as the untouched pre-cutover fallback.

If the Fibaro collector is enabled later, it receives the exact same bind mount in Compose.

## Rollback

Do not delete `defro-dashboard-thermoflow-db`. If the SSD deployment must be rolled back after the API has written new data, first stop the API/collector and create a verified SQLite backup from the SSD database, then restore that current database into the named volume before reverting the Compose mount. Simply switching back to the old named volume would discard writes made after cutover.
