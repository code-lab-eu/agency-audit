"""Row claims: give each pending website or city to exactly one worker.

The orchestrator, the MCP server and the dashboard share the database, so a
worker claims the rows it works on: one statement moves a website to
``auditing`` or a city to ``in_progress`` and records the claim time. The
selection uses ``FOR UPDATE SKIP LOCKED``, so concurrent claims never return
the same row and never wait for each other.

A worker that is killed leaves its claims behind; ``release_stale_claims``
returns claims older than ``AGENCY_AUDIT_CLAIM_TIMEOUT_MINUTES`` to ``pending``.
"""

from __future__ import annotations

import logging

import asyncpg

from agency_audit.config import settings
from agency_audit.db import get_pool

logger = logging.getLogger(__name__)


async def claim_websites(limit: int, country: str | None = None) -> list[asyncpg.Record]:
    """Claim up to *limit* pending websites for audit, oldest first.

    Args:
        limit: Maximum number of websites to claim.
        country: ISO code; when given, only websites linked to a city in that
            country are claimed.

    Returns:
        The claimed rows (id, url, label, maps_place_id, address, phone), now
        ``auditing``; empty when no pending website is left.
    """
    pool = await get_pool()
    claimed: list[asyncpg.Record] = await pool.fetch(
        """
        UPDATE websites
        SET audit_status = 'auditing', audit_claimed_at = now()
        WHERE id IN (
            SELECT w.id
            FROM websites w
            WHERE w.audit_status = 'pending'
              AND w.audit_attempts < 3
              AND ($2::text IS NULL OR EXISTS (
                  SELECT 1
                  FROM website_cities wc
                  JOIN cities c ON c.id = wc.city_id
                  WHERE wc.website_id = w.id AND c.country = $2
              ))
            ORDER BY w.id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, url, label, maps_place_id, address, phone
        """,
        limit,
        country.upper() if country else None,
    )
    return claimed


async def claim_next_city(country: str | None = None) -> asyncpg.Record | None:
    """Claim the largest pending city for discovery.

    Args:
        country: ISO code; when omitted, the largest pending city of any
            country is claimed.

    Returns:
        The claimed row (id, country, label, slug, population and the cached
        viewport columns), now ``in_progress``; ``None`` when no pending city
        is left.
    """
    pool = await get_pool()
    return await pool.fetchrow(
        """
        UPDATE cities
        SET discovery_status = 'in_progress', discovery_claimed_at = now()
        WHERE id = (
            SELECT id
            FROM cities
            WHERE discovery_status = 'pending'
              AND ($1::text IS NULL OR country = $1)
            ORDER BY population DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, country, label, slug, population,
                  viewport_low_lat, viewport_low_lng,
                  viewport_high_lat, viewport_high_lng
        """,
        country.upper() if country else None,
    )


async def release_stale_claims(timeout_minutes: int | None = None) -> dict[str, int]:
    """Return claims older than the timeout to ``pending``.

    Args:
        timeout_minutes: Claim age limit; defaults to
            ``AGENCY_AUDIT_CLAIM_TIMEOUT_MINUTES``.

    Returns:
        ``{"websites": n, "cities": m}``: the number of claims released.
    """
    if timeout_minutes is None:
        timeout_minutes = settings.claim_timeout_minutes

    pool = await get_pool()
    async with pool.acquire() as conn:
        websites = await conn.execute(
            """
            UPDATE websites SET audit_status = 'pending'
            WHERE audit_status = 'auditing'
              AND audit_claimed_at < now() - make_interval(mins => $1)
            """,
            timeout_minutes,
        )
        cities = await conn.execute(
            """
            UPDATE cities SET discovery_status = 'pending'
            WHERE discovery_status = 'in_progress'
              AND discovery_claimed_at < now() - make_interval(mins => $1)
            """,
            timeout_minutes,
        )

    released = {"websites": _row_count(websites), "cities": _row_count(cities)}
    logger.info(
        "Released %d website and %d city claims older than %d minutes",
        released["websites"],
        released["cities"],
        timeout_minutes,
    )
    return released


def _row_count(status: str) -> int:
    """Return the row count of a command status such as ``UPDATE 3``."""
    return int(status.rsplit(" ", 1)[-1])
