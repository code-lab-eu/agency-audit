-- 003_add_audit_log.sql — Add audit_log table and QC/review columns

-- =============================================================================
-- audit_log: tracks operational loop runs
-- =============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id               SERIAL PRIMARY KEY,
    country          TEXT REFERENCES countries(iso),
    run_type         TEXT NOT NULL CHECK (run_type IN ('discovery', 'audit', 'qc', 'reaudit', 'full_loop')),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    duration_seconds NUMERIC(8,2),
    items_processed  INTEGER NOT NULL DEFAULT 0,
    items_succeeded  INTEGER NOT NULL DEFAULT 0,
    items_failed     INTEGER NOT NULL DEFAULT 0,
    summary          JSONB DEFAULT '{}'::jsonb,
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_country ON audit_log (country);
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON audit_log (run_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_started ON audit_log (started_at DESC);

-- =============================================================================
-- websites: add QC and review tracking columns
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS review_reason TEXT;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS qc_checks JSONB DEFAULT '[]'::jsonb;

-- =============================================================================
-- websites: add retry tracking columns
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS audit_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS audit_last_error TEXT;

-- =============================================================================
-- discovery_log: add retry tracking
-- =============================================================================
ALTER TABLE discovery_log ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE discovery_log ADD COLUMN IF NOT EXISTS last_error TEXT;
