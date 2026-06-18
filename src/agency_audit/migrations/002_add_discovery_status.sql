-- 002_add_discovery_status.sql — Add discovery tracking columns

-- =============================================================================
-- cities: add discovery_status to track discovery pipeline state
-- =============================================================================
ALTER TABLE cities ADD COLUMN IF NOT EXISTS discovery_status TEXT
    NOT NULL DEFAULT 'pending'
    CHECK (discovery_status IN ('pending', 'in_progress', 'done', 'skipped'));

CREATE INDEX IF NOT EXISTS idx_cities_discovery_status
    ON cities (discovery_status);

-- =============================================================================
-- websites: add discovery metadata columns
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS maps_place_id TEXT;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE websites ADD COLUMN IF NOT EXISTS phone TEXT;
