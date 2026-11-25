-- ThermoFlow shared storage schema, version 1.
-- Raw source payloads are retained separately from derived features and model output.

CREATE TABLE telemetry_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    recorded_at INTEGER NOT NULL CHECK (recorded_at > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE (source, source_key)
);
CREATE INDEX idx_telemetry_source_time ON telemetry_samples(source, observed_at);

CREATE TABLE fibaro_events (
    event_id INTEGER PRIMARY KEY CHECK (event_id > 0),
    observed_at INTEGER NOT NULL CHECK (observed_at > 0),
    event_type TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    collected_at INTEGER NOT NULL CHECK (collected_at > 0)
);
CREATE INDEX idx_fibaro_events_time ON fibaro_events(observed_at);

CREATE TABLE fibaro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at INTEGER NOT NULL CHECK (captured_at > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);
CREATE INDEX idx_fibaro_snapshots_time ON fibaro_snapshots(captured_at);

CREATE TABLE refill_events (
    event_key TEXT PRIMARY KEY,
    occurred_at INTEGER NOT NULL CHECK (occurred_at > 0),
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);
CREATE INDEX idx_refill_events_time ON refill_events(occurred_at);

CREATE TABLE weather_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    forecast_for INTEGER NOT NULL CHECK (forecast_for > 0),
    fetched_at INTEGER NOT NULL CHECK (fetched_at > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE (source, forecast_for, fetched_at)
);
CREATE INDEX idx_weather_forecasts_target ON weather_forecasts(forecast_for, fetched_at);

CREATE TABLE feature_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_set TEXT NOT NULL,
    window_start INTEGER NOT NULL CHECK (window_start > 0),
    window_end INTEGER NOT NULL CHECK (window_end > window_start),
    generated_at INTEGER NOT NULL CHECK (generated_at > 0),
    dataset_version TEXT,
    values_json TEXT NOT NULL,
    UNIQUE (feature_set, window_start, window_end, dataset_version)
);
CREATE INDEX idx_feature_windows_range ON feature_windows(feature_set, window_start, window_end);

CREATE TABLE datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    manifest_json TEXT NOT NULL,
    content_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    UNIQUE (name, version)
);

CREATE TABLE model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    artifact_path TEXT,
    artifact_sha256 TEXT,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    UNIQUE (model_name, version)
);

CREATE TABLE model_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dataset_version TEXT,
    evaluated_at INTEGER NOT NULL CHECK (evaluated_at > 0),
    metrics_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX idx_model_evaluations_model_time ON model_evaluations(model_name, evaluated_at);

CREATE TABLE shadow_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    predicted_at INTEGER NOT NULL CHECK (predicted_at > 0),
    target_at INTEGER,
    prediction_json TEXT NOT NULL,
    actual_json TEXT,
    evaluation_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX idx_shadow_predictions_target ON shadow_predictions(model_name, target_at);

CREATE TABLE dashboard_cache (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    generated_at INTEGER NOT NULL CHECK (generated_at > 0),
    expires_at INTEGER
);
CREATE INDEX idx_dashboard_cache_expiry ON dashboard_cache(expires_at);

CREATE TABLE collector_state (
    collector_name TEXT PRIMARY KEY,
    cursor_value TEXT,
    updated_at INTEGER NOT NULL CHECK (updated_at > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collector_name TEXT NOT NULL,
    started_at INTEGER NOT NULL CHECK (started_at > 0),
    completed_at INTEGER,
    window_start INTEGER,
    window_end INTEGER,
    fetched INTEGER NOT NULL DEFAULT 0 CHECK (fetched >= 0),
    inserted INTEGER NOT NULL DEFAULT 0 CHECK (inserted >= 0),
    duplicates INTEGER NOT NULL DEFAULT 0 CHECK (duplicates >= 0),
    invalid INTEGER NOT NULL DEFAULT 0 CHECK (invalid >= 0),
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_collection_runs_name_time ON collection_runs(collector_name, started_at);
