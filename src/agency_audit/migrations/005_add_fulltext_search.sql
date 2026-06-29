-- 005_add_fulltext_search.sql — Full-text search on agency names
--
-- Adds a description column (stub for future integration — search only indexes
-- label until a population path is wired), a generated tsvector column on label,
-- and a GIN index for fast full-text search.

-- =============================================================================
-- websites: add description column (stub — populated later by discovery/audit)
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS description TEXT;

-- =============================================================================
-- websites: generated tsvector on label (name-only search for now)
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(label, '')), 'A')
    ) STORED;

-- =============================================================================
-- GIN index for fast @@ (match) queries
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_websites_search_vector
    ON websites USING GIN (search_vector);
