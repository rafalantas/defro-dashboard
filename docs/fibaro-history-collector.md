# Fibaro HC3 history collector

The collector in `thermoflow_fibaro_history_collector.py` is a pull-driven,
local persistence layer for the existing GET-only HC3 history provider.

## Safety and lifecycle

- Calls only `provider.get_events_range(start, end)`; the provider remains GET-only.
- Does not start a worker, timer, scheduler, or background poller. The caller
  explicitly chooses each bounded window and when collection runs.
- Uses SQLite transactions and the HC3 event ID as the unique key. Repeating
  an overlapping window is idempotent.
- Stores the raw event JSON, SHA-256 digest, event timestamp, type, and
  collection timestamp. Malformed events are counted, not silently promoted.
- Keeps a monotonic `last_successful_end` watermark. It is informational;
  callers must still choose overlap when resuming to protect against late
  arriving HC3 events.
- Retention is explicit through `prune()`; default is 90 days and the
  configured range is 1–3650 days.
- No HC3 credentials or secrets are written to the database.

## Storage

The API image receives a dedicated writable mount at
`/var/lib/thermoflow/fibaro`. This is separate from the existing read-only
`/run/thermoflow` mount used by model artifacts.

Environment:
- `FIBARO_HISTORY_DB_PATH`: SQLite file path; defaults to
  `/var/lib/thermoflow/fibaro/history.sqlite3`.

On the host, ensure `./data/fibaro-history` is writable by the API container
user and included in normal backup procedures. Do not put the database in the
read-only model-artifact volume.

## Example

```python
from thermoflow_fibaro_history_collector import FibaroHistoryStore

store = FibaroHistoryStore()
result = store.collect_once(provider, start=window_start, end=window_end)
```

This PR supplies the tested storage/collection core. It intentionally does not
enable autonomous polling or change API request behavior. A later integration
must add an explicit operational trigger, rate limits, monitoring, and a
recovery policy before production collection is enabled.
