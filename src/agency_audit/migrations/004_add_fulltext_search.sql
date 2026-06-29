-- 004_add_fulltext_search.sql — Full-text search on agency names and descriptions
--
-- Adds a description column (populated by discovery/audit pipelines) and a
-- generated tsvector column with a GIN index for fast full-text search.

-- =============================================================================
-- websites: add description column for agency prose
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS description TEXT;

-- =============================================================================
-- websites: generated tsvector with weighted fields (label = A, description = B)
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', COALESCE(label, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(description, '')), 'B')
    ) STORED;

-- =============================================================================
-- GIN index for fast @@ (match) queries
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_websites_search_vector
    ON websites USING GIN (search_vector);
