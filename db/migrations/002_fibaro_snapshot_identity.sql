-- Fibaro snapshot identity for idempotent snapshot ingestion.
-- Same timestamp + same canonical payload is a duplicate; changed snapshots remain distinct.
CREATE UNIQUE INDEX idx_fibaro_snapshots_identity
    ON fibaro_snapshots(captured_at, payload_sha256);
