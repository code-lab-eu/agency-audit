-- 005_add_city_viewport.sql — Add viewport bounding box columns for tiled discovery

-- Tiled discovery splits a city's area into a grid of small rectangles
-- and queries the Google Maps Places API for each tile.  Doing so needs
-- an accurate per-city bounding box.  These four nullable NUMERIC(9,6)
-- columns cache that box on the cities table.  They are populated lazily
-- on first discovery (a later task), so all four are nullable.
--
--   viewport_low_lat  / viewport_low_lng   → southwest corner
--   viewport_high_lat / viewport_high_lng  → northeast corner

ALTER TABLE cities ADD COLUMN IF NOT EXISTS viewport_low_lat  NUMERIC(9, 6);
ALTER TABLE cities ADD COLUMN IF NOT EXISTS viewport_low_lng  NUMERIC(9, 6);
ALTER TABLE cities ADD COLUMN IF NOT EXISTS viewport_high_lat NUMERIC(9, 6);
ALTER TABLE cities ADD COLUMN IF NOT EXISTS viewport_high_lng NUMERIC(9, 6);
