"""Operational loop — ties discovery and audit together for continuous processing.

Modules:
    orchestrator:  Country-by-country discovery scheduler, end-to-end loop execution
    qc:           Quality control checks (suspicious scores, duplicates, manual review)
    reaudit:      Re-audit scheduling (websites older than 30 days)
    tracking:     Progress tracking via discovery_log and audit_log
    retry:        Error recovery with exponential backoff (3 retries max)
"""

from agency_audit.loop.orchestrator import run_all_countries, run_country
from agency_audit.loop.qc import (
    detect_duplicates,
    flag_suspicious_scores,
    get_websites_needing_review,
    run_qc_checks,
)
from agency_audit.loop.reaudit import get_reaudit_queue, schedule_reaudits
from agency_audit.loop.retry import RetryConfig, mark_failed, retry
from agency_audit.loop.tracking import AuditLogEntry, get_progress, log_audit_run, log_discovery_run

__all__ = [
    "run_country",
    "run_all_countries",
    "flag_suspicious_scores",
    "detect_duplicates",
    "get_websites_needing_review",
    "run_qc_checks",
    "get_reaudit_queue",
    "schedule_reaudits",
    "AuditLogEntry",
    "log_discovery_run",
    "log_audit_run",
    "get_progress",
    "retry",
    "mark_failed",
    "RetryConfig",
]
