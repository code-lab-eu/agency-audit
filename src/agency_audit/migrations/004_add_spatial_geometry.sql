-- 004_add_spatial_geometry.sql — Add PostGIS geometry column for spatial queries
--
-- This migration:
--   1. Enables the PostGIS extension (required for geometry types)
--   2. Adds a location column (Point, SRID 4326 — WGS 84) to the websites table
--   3. Creates a GiST spatial index for fast bounding-box queries

-- =============================================================================
-- Enable PostGIS extension
-- =============================================================================
CREATE EXTENSION IF NOT EXISTS postgis;

-- =============================================================================
-- websites: add location column (WGS 84 Point)
-- =============================================================================
ALTER TABLE websites ADD COLUMN IF NOT EXISTS location geometry(Point, 4326);

-- =============================================================================
-- Spatial index for bounding-box queries via && operator
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_websites_location ON websites USING GIST (location);
