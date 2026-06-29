-- 004_add_failed_discovery_status.sql — allow 'failed' as a terminal city discovery status

-- The UI can trigger discovery for a single city as a background task. When that
-- task raises, the row is marked 'failed' so the spinner/polling loop stops and
-- the user can retry. The original CHECK constraint (002) did not allow 'failed',
-- so widen it here.
ALTER TABLE cities DROP CONSTRAINT IF EXISTS cities_discovery_status_check;
ALTER TABLE cities ADD CONSTRAINT cities_discovery_status_check
    CHECK (discovery_status IN ('pending', 'in_progress', 'done', 'skipped', 'failed'));
