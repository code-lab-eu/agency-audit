"""Progress tracking for the operational loop.

Logs discovery and audit runs to the audit_log table with timestamps,
durations, success/failure counts, and structured summaries.

Also provides overall progress queries (how many cities/websites processed,
by country, with timing stats).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)


@dataclass
class AuditLogEntry:
    """Represents a row in the audit_log table."""

    id: int | None = None
    country: str | None = None
    run_type: str = "full_loop"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    items_processed: int = 0
    items_succeeded: int = 0
    items_failed: int = 0
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Logging helpers
# ──────────────────────────────────────────────────────────────────────


async def log_discovery_run(
    country: str,
    cities_processed: int,
    agencies_found: int,
    duration_seconds: float,
    errors: list[str] | None = None,
) -> int:
    """Log a discovery run for a country.

    Returns the audit_log ID.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        log_id = await conn.fetchval(
            """INSERT INTO audit_log
               (country, run_type, finished_at, duration_seconds,
                items_processed, items_succeeded, items_failed, summary)
               VALUES ($1, 'discovery', now(), $2, $3, $4, $5, $6::jsonb)
               RETURNING id""",
            country,
            round(duration_seconds, 2),
            cities_processed,
            agencies_found,
            len(errors) if errors else 0,
            _make_json(
                {
                    "cities_processed": cities_processed,
                    "agencies_found": agencies_found,
                    "errors": errors or [],
                }
            ),
        )
    logger.info(
        "Discovery run logged: %s — %d cities, %d agencies (%.1fs)",
        country,
        cities_processed,
        agencies_found,
        duration_seconds,
    )
    return int(log_id)


async def log_audit_run(
    website_id: int,
    score: int,
    duration_seconds: float,
    country: str | None = None,
    success: bool = True,
    error: str | None = None,
) -> int:
    """Log an individual website audit run.

    Returns the audit_log ID.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Resolve country if not provided
        if country is None:
            country = await conn.fetchval(
                """SELECT c.country
                   FROM website_cities wc
                   JOIN cities c ON c.id = wc.city_id
                   WHERE wc.website_id = $1
                   LIMIT 1""",
                website_id,
            )

        log_id = await conn.fetchval(
            """INSERT INTO audit_log
               (country, run_type, finished_at, duration_seconds,
                items_processed, items_succeeded, items_failed, summary, error)
               VALUES ($1, 'audit', now(), $2, 1, $3, $4, $5::jsonb, $6)
               RETURNING id""",
            country,
            round(duration_seconds, 2),
            1 if success else 0,
            0 if success else 1,
            _make_json({"website_id": website_id, "score": score}),
            error,
        )
    return int(log_id)


async def log_full_loop_run(
    country: str | None,
    cities_processed: int,
    agencies_discovered: int,
    websites_audited: int,
    audits_succeeded: int,
    audits_failed: int,
    qc_findings: int,
    reaudit_queued: int,
    duration_seconds: float,
    errors: list[str] | None = None,
) -> int:
    """Log a full operational loop run."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        log_id = await conn.fetchval(
            """INSERT INTO audit_log
               (country, run_type, finished_at, duration_seconds,
                items_processed, items_succeeded, items_failed, summary)
               VALUES ($1, 'full_loop', now(), $2, $3, $4, $5, $6::jsonb)
               RETURNING id""",
            country,
            round(duration_seconds, 2),
            cities_processed + websites_audited,
            agencies_discovered + audits_succeeded,
            audits_failed + len(errors) if errors else audits_failed,
            _make_json(
                {
                    "cities_processed": cities_processed,
                    "agencies_discovered": agencies_discovered,
                    "websites_audited": websites_audited,
                    "audits_succeeded": audits_succeeded,
                    "audits_failed": audits_failed,
                    "qc_findings": qc_findings,
                    "reaudit_queued": reaudit_queued,
                    "errors": errors or [],
                }
            ),
        )
    return int(log_id)


# ──────────────────────────────────────────────────────────────────────
# Progress queries
# ──────────────────────────────────────────────────────────────────────


async def get_progress() -> dict[str, Any]:
    """Get overall progress across the entire pipeline.

    Returns:
        Dict with per-country and overall stats.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Overall counts
        total_countries = await conn.fetchval("SELECT COUNT(*) FROM countries WHERE active = true")
        total_cities = await conn.fetchval("SELECT COUNT(*) FROM cities")
        cities_done = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        cities_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'pending'"
        )

        # Website stats
        total_websites = await conn.fetchval("SELECT COUNT(*) FROM websites")
        websites_audited = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'audited'"
        )
        websites_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'pending'"
        )
        websites_failed = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'failed'"
        )
        websites_needing_review = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE needs_review = true"
        )

        # Average score
        avg_score = await conn.fetchval(
            "SELECT ROUND(AVG(score), 1) FROM websites WHERE audit_status = 'audited'"
        )

        # Per-country breakdown
        per_country = await conn.fetch(
            """SELECT
                   c.iso,
                   c.label,
                   COUNT(DISTINCT ci.id) AS total_cities,
                   COUNT(DISTINCT ci.id) FILTER (WHERE ci.discovery_status = 'done') AS cities_done,
                   COUNT(DISTINCT w.id) AS total_websites,
                   COUNT(DISTINCT w.id) FILTER (WHERE w.audit_status = 'audited')
                       AS websites_audited,
                   ROUND(AVG(w.score) FILTER (WHERE w.audit_status = 'audited'), 1) AS avg_score
               FROM countries c
               LEFT JOIN cities ci ON ci.country = c.iso
               LEFT JOIN website_cities wc ON wc.city_id = ci.id
               LEFT JOIN websites w ON w.id = wc.website_id
               WHERE c.active = true
               GROUP BY c.iso, c.label
               ORDER BY c.label"""
        )

        # Recent runs
        recent_runs = await conn.fetch(
            """SELECT id, country, run_type, started_at, finished_at,
                      duration_seconds, items_processed, items_succeeded, items_failed, summary
               FROM audit_log
               ORDER BY started_at DESC
               LIMIT 20"""
        )

    return {
        "overview": {
            "countries": total_countries,
            "cities_total": total_cities,
            "cities_done": cities_done,
            "cities_pending": cities_pending,
            "websites_total": total_websites,
            "websites_audited": websites_audited,
            "websites_pending": websites_pending,
            "websites_failed": websites_failed,
            "websites_needing_review": websites_needing_review,
            "avg_score": float(avg_score) if avg_score else 0,
        },
        "per_country": [
            {
                "iso": r["iso"],
                "label": r["label"],
                "total_cities": r["total_cities"],
                "cities_done": r["cities_done"],
                "total_websites": r["total_websites"],
                "websites_audited": r["websites_audited"],
                "avg_score": float(r["avg_score"]) if r["avg_score"] else 0,
            }
            for r in per_country
        ],
        "recent_runs": [
            {
                "id": r["id"],
                "country": r["country"],
                "run_type": r["run_type"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "duration_seconds": float(r["duration_seconds"]) if r["duration_seconds"] else 0,
                "items_processed": r["items_processed"],
                "items_succeeded": r["items_succeeded"],
                "items_failed": r["items_failed"],
            }
            for r in recent_runs
        ],
    }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_json(obj: Any) -> str:
    """Serialize an object to JSON string for JSONB storage."""
    return json.dumps(obj, default=str)
