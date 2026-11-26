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
boiler controller:

```sh
docker exec -i defro-dashboard-api \
  python -m thermoflow_burner_artifact_store \
  /var/lib/thermoflow/thermoflow.sqlite3 \
  /dev/stdin /tmp/burner-benchmark.json /tmp/burner-model.json
```

The command above is illustrative only: the three JSON input files must be
available inside the container. Operators may instead run the module in the
environment where those files and the database are available.

Repeated imports of the same dataset/model are idempotent. Changed source
snapshots create a new dataset version. The holdout is stored with its actual
FIRE-confirmed duration and the model's offline estimate.

## Runtime boundary

This step adds persistence and checksum-verified loading primitives. It does
not yet switch the live API runtime from its read-only artifact-file mount to
SQLite, nor does it start any collector. Those are separate integration steps
with their own tests and rollout. Existing API behavior remains unchanged.

## Validation

```sh
python -m unittest discover -s tests -p 'test_thermoflow_burner_artifact_store.py' -v
```
