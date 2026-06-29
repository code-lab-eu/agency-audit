"""Full-text search for agencies using PostgreSQL tsvector/tsquery.

Provides a single async entry point ``search_agencies()`` that queries
the ``websites`` table against a GIN-indexed generated tsvector column.
No dependency on geometry, viewport, or spatial filters — those can be
layered on later.

The tsvector is built from ``label`` (weight A) and ``description``
(weight B) in descending priority, so hits on the agency name rank
higher than hits on the description.

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

_DEFAULT_SEARCH_LIMIT = 20


async def search_agencies(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Full-text search across agency names and descriptions.

    Uses PostgreSQL ``plainto_tsquery('english', …)`` for tolerant
    query parsing (AND semantics, stop-word removal, stemming) and
    ``ts_rank()`` for relevance ordering.  The query is matched against
    the generated ``search_vector`` column backed by a GIN index.

    Args:
        query: Free-text search term (e.g. ``"berlin immobilien"``).
        limit: Maximum number of results (default 20).

    Returns:
        List of dicts with keys ``id``, ``url``, ``label``,
        ``description``, ``score``, ``audit_status``, and ``rank``
        (float, higher is more relevant), ordered by ``rank DESC``.
        Empty list when the query is blank.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

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
            limit,
        )

    return [dict(row) for row in rows]
