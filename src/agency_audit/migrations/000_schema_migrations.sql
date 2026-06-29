-- 000_schema_migrations.sql — Bootstrap migration tracking table
-- This MUST sort before all other migrations so the ledger exists
-- before run_migrations() checks it.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
