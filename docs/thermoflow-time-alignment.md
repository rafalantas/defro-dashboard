# ThermoFlow shared time model

All source observations are normalized to Unix seconds and labeled in UTC plus the configured local IANA timezone (default `Europe/Warsaw`). Local timestamps retain their UTC offset, including daylight-saving transitions.

Fibaro event transitions are aligned to the nearest actual eModul telemetry sample. A match is accepted only within the configured tolerance (default 90 seconds). The signed delta is `eModul timestamp - Fibaro timestamp`.

No interpolation, forward fill, synthetic zero, or missing-sample creation is permitted. Unmatched Fibaro events remain in the result with `emodul: null`. Historical head-to-zone mapping remains explicitly unverified and is not reconstructed by time alignment.
