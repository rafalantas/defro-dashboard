# ThermoFlow shared time model

All source observations are normalized to Unix seconds and labeled in UTC plus an IANA local timezone (default `Europe/Warsaw`). Local timestamps retain their UTC offset, including daylight-saving transitions.

Fibaro transitions are aligned independently to the nearest actual sample from each available telemetry source (eModul and ThingSpeak). A match is accepted only within the configured tolerance (default 90 seconds); the signed delta is `telemetry timestamp - Fibaro timestamp`.

Samples are allocated one-to-one per source event and telemetry source. Multiple signal transitions belonging to the same Fibaro source event may share its matched sample; a distinct Fibaro event cannot reuse that sample for the same source. Matching is deterministic and preserves source keys.

ThingSpeak's archived `field1`..`field8` values remain named exactly as received. The time-alignment layer does not guess field meanings or units; that requires trusted channel metadata.

No interpolation, forward fill, synthetic zero, or missing-sample creation is permitted. Unmatched Fibaro events remain in the result. Historical head-to-zone mapping remains explicitly unverified and is not reconstructed by time alignment.
