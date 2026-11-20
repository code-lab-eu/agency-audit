-- 009_add_claimed_at.sql — When a worker claimed a website or a city
--
-- Set when a worker moves a website to ``audit_status = 'auditing'`` or a city
-- to ``discovery_status = 'in_progress'``. A claim older than
-- ``AGENCY_AUDIT_CLAIM_TIMEOUT_MINUTES`` belongs to a worker that was killed,
-- and ``release_stale_claims`` returns the row to ``pending``.
--
-- Rows claimed before this migration get the migration time, so they are
-- released after the timeout like any other claim.

ALTER TABLE websites ADD COLUMN audit_claimed_at TIMESTAMPTZ;
ALTER TABLE cities ADD COLUMN discovery_claimed_at TIMESTAMPTZ;

UPDATE websites SET audit_claimed_at = now() WHERE audit_status = 'auditing';
UPDATE cities SET discovery_claimed_at = now() WHERE discovery_status = 'in_progress';
