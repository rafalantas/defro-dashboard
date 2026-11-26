# ThermoFlow observed heating aggregates

The aggregate builder produces hourly and daily buckets from actual aligned Fibaro transitions. It does not create empty hours/days or infer observations during quiet periods.

- Fibaro transitions are counted as events.
- A reused eModul sample is counted once per bucket for telemetry coverage and metric summaries.
- Numeric values are restricted to a small heating allow-list (boiler/return/flue/outdoor/DHW/buffer temperatures, boiler power, valve, pumps, and fan).
- Each metric includes its observation count, mean, minimum, and maximum.
- Hour keys include the UTC offset so the repeated autumn daylight-saving hour is not collapsed.

An unmatched Fibaro transition still contributes to event counts but contributes no fabricated eModul values. Aggregates are derived output and do not duplicate source samples in SQLite.
