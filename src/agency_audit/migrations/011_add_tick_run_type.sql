-- 011_add_tick_run_type.sql — Log `agency-audit tick` runs in audit_log

ALTER TABLE audit_log DROP CONSTRAINT audit_log_run_type_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_run_type_check
    CHECK (run_type IN ('discovery', 'audit', 'qc', 'reaudit', 'full_loop', 'tick'));
