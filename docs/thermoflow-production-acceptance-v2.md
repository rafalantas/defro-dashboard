# ThermoFlow Production Acceptance 2.0

The read-only endpoint /api/thermoflow/v1/acceptance projects the available
shadow telemetry, Burner/Fuel prediction states, optional offline data-quality
and model-monitoring reports, and explicit operator deployment checks.

Each gate is ready, shadow_ready, collecting, blocked or not_assessed. Missing
evidence is never interpreted as success. A predicted model output is only
shadow_ready and still requires manual review. CI status is deliberately not
used as a proxy for live production health.

Deployment checks include API health, intelligence endpoint, HTTP 5xx,
frontend smoke, desktop/mobile review and container rollout. These are passed
only when an operator explicitly supplies true. False is a blocker; absent is
not_assessed. Full status means ready_for_manual_signoff, not automatic
certification or model promotion.

The endpoint performs no upstream I/O and does not alter any controller state.
AI/ML remains advisory and read-only. Return-temperature protection remains
55°C ±1°C.
