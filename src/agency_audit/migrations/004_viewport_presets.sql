-- 004_viewport_presets.sql — Store saved map viewport presets (for the web dashboard)
-- Independent module — no dependencies on search or geometry modules.
-- Uses its own DB access pattern via get_pool().

CREATE TABLE IF NOT EXISTS viewport_presets (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT,                          -- optional user identifier
    name        TEXT NOT NULL,                 -- human-readable preset name
    center_lat  DOUBLE PRECISION NOT NULL,     -- center latitude
    center_lng  DOUBLE PRECISION NOT NULL,     -- center longitude
    zoom_level  INTEGER NOT NULL,             -- map zoom level
    north       DOUBLE PRECISION NOT NULL,    -- bounding box north
    south       DOUBLE PRECISION NOT NULL,    -- bounding box south
    east        DOUBLE PRECISION NOT NULL,    -- bounding box east
    west        DOUBLE PRECISION NOT NULL,    -- bounding box west
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_viewport_presets_user ON viewport_presets (user_id);
CREATE INDEX IF NOT EXISTS idx_viewport_presets_created ON viewport_presets (created_at DESC);
