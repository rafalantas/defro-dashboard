# Burner ML — SQLite persistence

## Scope

This is an explicit offline import of an already evaluated Burner ML run into
the shared ThermoFlow SQLite database. It persists:

- chronological training and holdout feature rows in `feature_windows`;
- a content-addressed dataset manifest in `datasets`;
- the validated read-only model artifact and checksum in `model_versions`;
- benchmark metrics and baseline comparisons in `model_evaluations`;
- per-cycle holdout predictions paired with FIRE-confirmed actual durations in
  `shadow_predictions`.

The raw FIRE snapshot is not duplicated by this importer. It remains a separate
source-history ingestion responsibility. Model binaries are not stored; the
small JSON inference artifact is stored as versioned metadata and verified by
SHA-256 when loaded.

## Explicit import

The database must already exist and have the current schema. This command does
not run migrations, fetch data, train a model, promote a model, or touch the
boiler controller.

Copy the three local JSON files into the running API container, then import
them into the existing SSD-backed database:

```sh
docker cp "$HOME/observed-cycles.json" defro-dashboard-api:/tmp/observed-cycles.json
docker cp "$HOME/burner-benchmark.json" defro-dashboard-api:/tmp/burner-benchmark.json
docker cp "$HOME/burner-model.json" defro-dashboard-api:/tmp/burner-model.json

docker exec defro-dashboard-api python -m thermoflow_burner_artifact_store \
  /var/lib/thermoflow/thermoflow.sqlite3 \
  /tmp/observed-cycles.json \
  /tmp/burner-benchmark.json \
  /tmp/burner-model.json
```

Repeated imports of the same dataset/model are idempotent. Changed source
snapshots create a new dataset version. Training and holdout rows are stored
as compact feature windows; the holdout is paired with its model estimate and
FIRE-confirmed actual duration. The artifact is stored as versioned JSON
metadata with a SHA-256 checksum.

## One-command offline registration

After producing `observed-cycles.json`, the benchmark, artifact build and
strict SQLite import can be run as one explicit command:

```sh
docker cp "$HOME/observed-cycles.json" defro-dashboard-api:/tmp/observed-cycles.json
docker exec defro-dashboard-api python -m thermoflow_burner_registration \
  /var/lib/thermoflow/thermoflow.sqlite3 \
  /tmp/observed-cycles.json
```

The command does not run from the scheduler. It skips without writes when the
benchmark is not evaluated, and the existing importer still verifies source
checksums, chronological partitions, model contract and the untouched-holdout
baseline gate. Promotion remains a manual review decision.

## Runtime boundary

This step adds persistence and checksum-verified loading primitives. It does
not yet switch the live API runtime from its read-only artifact-file mount to
SQLite, nor does it start any collector. Those are separate integration steps
with their own tests and rollout. Existing API behavior remains unchanged.

## Validation

```sh
python -m unittest discover -s tests -p 'test_thermoflow_burner_artifact_store.py' -v
```
