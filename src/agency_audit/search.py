"""Full-text search for agencies using PostgreSQL tsvector/tsquery.

Provides two public entry points:

    ``search_agencies(query, limit=20)`` — full-text search against agency
    names using a GIN-indexed tsvector column.

    ``set_agency_description(website_id, description)`` — populate the
    description column (stub for future integration; not yet indexed).

No dependency on geometry, viewport, or spatial filters — those can be
layered on later.

The tsvector is currently built from ``label`` only (weight A).
When a description population path is wired, the migration will be
expanded to include description in the tsvector with weight B.

Example::

    results = await search_agencies("immobilienmakler berlin", limit=10)
    for row in results:
        print(row["label"], row["rank"])
"""

from __future__ import annotations

import logging
from typing import Any

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)

_MIN_SEARCH_LIMIT = 1
_DEFAULT_SEARCH_LIMIT = 20
_MAX_SEARCH_LIMIT = 200


def _clamp_limit(limit: int) -> int:
    """Clamp the search limit to a safe range."""
    return max(_MIN_SEARCH_LIMIT, min(limit, _MAX_SEARCH_LIMIT))


async def search_agencies(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Full-text search across agency names.

    Uses PostgreSQL ``plainto_tsquery('english', …)`` for tolerant
    query parsing (AND semantics, stop-word removal, stemming) and
    ``ts_rank()`` for relevance ordering.  The query is matched against
    the generated ``search_vector`` column backed by a GIN index.

    Args:
        query: Free-text search term (e.g. ``"berlin immobilien"``).
        limit: Maximum number of results. Clamped to 1–200
            (default 20).

    Returns:
        List of dicts with keys ``id``, ``url``, ``label``,
        ``description``, ``score``, ``audit_status``, and ``rank``
        (float, higher is more relevant), ordered by ``rank DESC``.
        Empty list when the query is blank.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

    safe_limit = _clamp_limit(limit)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, url, label, description, score, audit_status,
                   ts_rank(search_vector, query) AS rank
            FROM websites, plainto_tsquery('english', $1) query
            WHERE search_vector @@ query
            ORDER BY rank DESC
            LIMIT $2
            """,
            cleaned,
            safe_limit,
        )

    return [dict(row) for row in rows]


async def set_agency_description(website_id: int, description: str) -> None:
    """Set the description column for an existing agency website.

    This is a forward-looking stub.  Once the description is populated
    by the discovery or audit pipelines and the search_vector migration
    is expanded to include it, descriptions will contribute to search
    relevance.

    Args:
        website_id: ID of the website row to update.
        description: Prose description of the agency.
    """
    cleaned = description.strip() if description else ""
    value = cleaned if cleaned else None

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE websites SET description = $1 WHERE id = $2",
            value,
            website_id,
        )
