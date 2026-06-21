"""Re-audit scheduling for websites audited more than 30 days ago.

Websites need periodic re-audits to verify they're still scrapeable and
their score remains accurate. This module identifies websites overdue for
re-audit and queues them (sets audit_status back to 'pending').
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)

# Default re-audit interval: 30 days
DEFAULT_REAUDIT_INTERVAL_DAYS = 30

# Max websites to queue per re-audit run (prevent overwhelming the system)
MAX_REAUDIT_BATCH = 500


# ──────────────────────────────────────────────────────────────────────
# Re-audit queue
# ──────────────────────────────────────────────────────────────────────


async def get_reaudit_queue(
    interval_days: int = DEFAULT_REAUDIT_INTERVAL_DAYS,
    limit: int = MAX_REAUDIT_BATCH,
    country: str | None = None,
) -> list[dict[str, Any]]:
    """Get websites that are overdue for re-audit.

    Criteria:
      - audit_status = 'audited'
      - last_audited_at < now() - interval_days (or NULL if never audited)
      - NOT marked as failed or needing review

    Args:
        interval_days: Number of days since last audit to trigger re-audit.
        limit: Maximum number of websites to return.
        country: Optional country ISO code to filter by.

    Returns:
        List of website dicts with id, url, score, last_audited_at, age_days.
    """
    pool = await get_pool()
    cutoff = datetime.now(UTC) - timedelta(days=interval_days)

    query = """SELECT w.id, w.url, w.label, w.score,
                      w.last_audited_at,
                      EXTRACT(DAY FROM now() - w.last_audited_at)::int AS age_days,
                      c.country
               FROM websites w
               JOIN website_cities wc ON wc.website_id = w.id
               JOIN cities c ON c.id = wc.city_id
               WHERE w.audit_status = 'audited'
                 AND w.needs_review = false
                 AND (w.last_audited_at < $1 OR w.last_audited_at IS NULL)
                 AND w.audit_attempts < 3"""
    params: list[Any] = [cutoff]

    if country:
        query += " AND c.country = $2"
        params.append(country)

    query += " ORDER BY w.last_audited_at ASC NULLS FIRST LIMIT $" + str(len(params) + 1)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    websites = [
        {
            "id": row["id"],
            "url": row["url"],
            "label": row["label"],
            "score": row["score"],
            "last_audited_at": row["last_audited_at"].isoformat()
            if row["last_audited_at"]
            else None,
            "age_days": int(row["age_days"]) if row["age_days"] else None,
            "country": row["country"],
        }
        for row in rows
    ]

    logger.info("Re-audit queue: %d websites overdue (>%d days)", len(websites), interval_days)
    return websites


async def schedule_reaudits(
    interval_days: int = DEFAULT_REAUDIT_INTERVAL_DAYS,
    limit: int = MAX_REAUDIT_BATCH,
    country: str | None = None,
) -> dict[str, Any]:
    """Queue overdue websites for re-audit and log the run.

    Sets audit_status back to 'pending' for each overdue website.

    Args:
        interval_days: Number of days since last audit to trigger re-audit.
        limit: Maximum number of websites to queue.
        country: Optional country ISO code to filter by.

    Returns:
        Summary dict with count of queued websites and oldest age.
    """
    pool = await get_pool()
    cutoff = datetime.now(UTC) - timedelta(days=interval_days)

    async with pool.acquire() as conn:
        # Get the websites to re-audit
        rows = await conn.fetch(
            """SELECT w.id, w.url, w.score,
                      EXTRACT(DAY FROM now() - w.last_audited_at)::int AS age_days
               FROM websites w
               WHERE w.audit_status = 'audited'
                 AND w.needs_review = false
                 AND (w.last_audited_at < $1 OR w.last_audited_at IS NULL)
                 AND w.audit_attempts < 3
               ORDER BY w.last_audited_at ASC NULLS FIRST
               LIMIT $2""",
            cutoff,
            limit,
        )

        if not rows:
            logger.info("No websites overdue for re-audit")
            return {"queued": 0, "oldest_age_days": None}

        website_ids = [row["id"] for row in rows]

        # Reset status to pending and increment attempt counter
        await conn.execute(
            """UPDATE websites
               SET audit_status = 'pending',
                   audit_attempts = audit_attempts + 1,
                   last_audited_at = NULL
               WHERE id = ANY($1)""",
            website_ids,
        )

        # Log the re-audit scheduling
        oldest_age = max(row["age_days"] for row in rows if row["age_days"])
        country_val = country or "all"

        await conn.execute(
            """INSERT INTO audit_log
               (country, run_type, finished_at, duration_seconds,
                items_processed, items_succeeded, summary)
               VALUES ($1, 'reaudit', now(), 0, $2, $2, $3::jsonb)""",
            country_val,
            len(website_ids),
            _make_json(
                {
                    "queued_websites": len(website_ids),
                    "oldest_age_days": oldest_age,
                    "interval_days": interval_days,
                    "country": country_val,
                }
            ),
        )

    logger.info(
        "Re-audit scheduled: %d websites queued (oldest: %d days)",
        len(website_ids),
        oldest_age,
    )
    return {"queued": len(website_ids), "oldest_age_days": oldest_age}


def _make_json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
