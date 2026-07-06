-- 006_add_discovery_error.sql — Store the reason a city's discovery failed
--
-- When ``discover_city`` raises (e.g. viewport resolution failure), the
-- exception message is persisted here alongside ``discovery_status = 'failed'``
-- so the web UI and MCP server can surface the reason rather than showing
-- a misleading ``done`` count or a bare ``failed`` badge.
-- Cleared to NULL when discovery starts or succeeds.

ALTER TABLE cities ADD COLUMN IF NOT EXISTS discovery_error TEXT;
