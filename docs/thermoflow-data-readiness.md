# ThermoFlow data readiness audit

The first AI/ML delivery gate is evidence from real, provenance-labelled snapshots.
The audit is offline and read-only; it does not call the live API, write ThingSpeak,
persist models, or change boiler/controller settings.

## Prepare source snapshots

Use the existing importers with the installation's configured read-only ThingSpeak
credentials. Keep the original JSON files private:

```sh
python3 -m thermoflow_fire_snapshot --days 14 > fire-snapshot.json
python3 -m thermoflow_fuel_snapshot --days 365 --status
python3 -m thermoflow_fuel_snapshot --days 365 > verified-fuel-periods.json
```

Fuel labels are considered verified only when refill IDs are explicitly supplied
through trusted import metadata. A controller fuel-percent reset is boundary
context; it does not prove the physical mass added. Do not use
`--assume-equal-fill` for a production benchmark. The fuel importer also needs
an archived as-issued baseline; a replay reconstructed from later observations
is not equivalent evidence.

The weather snapshot must contain the same verified fuel periods and archived
forecast records, with forecast issue/availability timestamps preserved. A
reanalysis or observed temperature substituted after the refill period is not
an as-issued forecast.

## Run the audit

```sh
python3 -m thermoflow_data_readiness \
  --burner fire-snapshot.json \
  --fuel verified-fuel-periods.json \
  --weather verified-fuel-periods-with-forecasts.json \
  > thermoflow-readiness.json
```

Each source is optional, but at least one is required. The report contains only
aggregate counts, quality exclusions, source-window metadata and benchmark
states; it never copies raw samples, refill IDs, API keys or model coefficients.
The existing chronological evaluators apply their own gates. A module is ready
only when its evaluator reaches `evaluated`; the minimum is 25 eligible samples
with a five-sample newest holdout. Weather additionally requires 25 paired
verified periods.

## Interpretation and limits

- `ready` means the supplied historical benchmark passed current data and
  evaluation gates. It does not enable live prediction or automatic promotion.
- `collecting` means more valid evidence or a missing input is required.
- `unavailable` means the evaluator rejected the supplied evidence or could
  not produce a usable benchmark.
- The audit cannot independently prove that refill metadata is physically
  correct. Keep that provenance review outside the model.
- The current live shadow runtime remains limited to existing flue/return
  screening. Pump and 4D-valve histories need a separately validated aligned
  telemetry dataset before they can become ML features.
- Boiler protection and control remain outside this project. Return protection
  remains 55°C ±1°C and is not changed by any ML stage.
