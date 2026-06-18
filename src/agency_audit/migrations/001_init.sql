-- 001_init.sql — Initial schema for agency-audit
-- Tables: countries, cities, websites, website_cities, discovery_log

-- =============================================================================
-- countries
-- =============================================================================
CREATE TABLE IF NOT EXISTS countries (
    iso    TEXT PRIMARY KEY,
    label  TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true
);

-- =============================================================================
-- cities
-- =============================================================================
CREATE TABLE IF NOT EXISTS cities (
    id         SERIAL PRIMARY KEY,
    country    TEXT NOT NULL REFERENCES countries(iso) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    slug       TEXT NOT NULL,
    population INTEGER NOT NULL,
    latitude   NUMERIC(9, 6),
    longitude  NUMERIC(9, 6)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cities_country_slug ON cities (country, slug);
CREATE INDEX IF NOT EXISTS idx_cities_country_pop ON cities (country, population DESC);

-- =============================================================================
-- websites
-- =============================================================================
CREATE TABLE IF NOT EXISTS websites (
    id              SERIAL PRIMARY KEY,
    url             TEXT UNIQUE NOT NULL,
    label           TEXT,
    score           INTEGER NOT NULL DEFAULT 0,
    audit_data      JSONB DEFAULT '{}'::jsonb,
    audit_status    TEXT DEFAULT 'pending'
                        CHECK (audit_status IN ('pending', 'auditing', 'audited', 'failed')),
    last_audited_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_websites_status ON websites (audit_status);
CREATE INDEX IF NOT EXISTS idx_websites_score ON websites (score DESC);

-- =============================================================================
-- website_cities (junction table)
-- =============================================================================
CREATE TABLE IF NOT EXISTS website_cities (
    website_id     INTEGER NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
    city_id        INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    discovered_via TEXT,
    PRIMARY KEY (website_id, city_id)
);

-- =============================================================================
-- discovery_log
-- =============================================================================
CREATE TABLE IF NOT EXISTS discovery_log (
    id           SERIAL PRIMARY KEY,
    city_id      INTEGER REFERENCES cities(id) ON DELETE SET NULL,
    website_id   INTEGER REFERENCES websites(id) ON DELETE SET NULL,
    agent        TEXT,
    search_query TEXT,
    status       TEXT CHECK (status IN ('searched', 'found', 'skipped', 'failed')),
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_discovery_city ON discovery_log (city_id);
CREATE INDEX IF NOT EXISTS idx_discovery_status ON discovery_log (status);
