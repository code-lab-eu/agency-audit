"""Spatial / geometry helpers for agency-audit.

Self-contained module providing bounding-box queries and location
management over the ``websites.location`` geometry column.

All functions are async and accept an asyncpg connection or pool.
"""

from __future__ import annotations

from typing import Any

import asyncpg


async def query_by_bounding_box(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    *,
    conn: asyncpg.Connection | None = None,
    pool: asyncpg.Pool | None = None,
) -> list[dict[str, Any]]:
    """Return websites whose ``location`` falls within the given bounding box.

    The bounding box is defined by min/max latitude and longitude (WGS 84).
    Uses the ``&&`` operator with ``ST_MakeEnvelope`` for efficient spatial
    filtering.

    One of *conn* or *pool* must be provided.  When only *pool* is given a
    connection is acquired and released automatically.
    """
    if conn is not None:
        return await _query_by_bbox(conn, min_lat, min_lng, max_lat, max_lng)

    if pool is not None:
        async with pool.acquire() as c:
            return await _query_by_bbox(c, min_lat, min_lng, max_lat, max_lng)  # type: ignore[arg-type]

    raise ValueError("Either conn or pool must be provided")


async def _query_by_bbox(
    conn: asyncpg.Connection,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, url, label, score, audit_data, audit_status,
               last_audited_at, created_at,
               ST_AsText(location) AS location_wkt
        FROM websites
        WHERE location IS NOT NULL
          AND location && ST_MakeEnvelope($1, $2, $3, $4, 4326)
        ORDER BY id
        """,
        min_lng,
        min_lat,
        max_lng,
        max_lat,
    )
    return [dict(row) for row in rows]


async def set_location(
    website_id: int,
    lat: float,
    lng: float,
    *,
    conn: asyncpg.Connection | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """Set or update the ``location`` geometry column for a website.

    Uses ``ST_SetSRID(ST_MakePoint(lng, lat), 4326)`` to produce a
    WGS 84 Point.
    """
    if conn is not None:
        await _set_location(conn, website_id, lat, lng)
        return

    if pool is not None:
        async with pool.acquire() as c:
            await _set_location(c, website_id, lat, lng)  # type: ignore[arg-type]
        return

    raise ValueError("Either conn or pool must be provided")


async def _set_location(
    conn: asyncpg.Connection,
    website_id: int,
    lat: float,
    lng: float,
) -> None:
    await conn.execute(
        """
        UPDATE websites
        SET location = ST_SetSRID(ST_MakePoint($1, $2), 4326)
        WHERE id = $3
        """,
        lng,
        lat,
        website_id,
    )


async def bulk_set_locations(
    rows: list[tuple[int, float, float]],
    *,
    conn: asyncpg.Connection | None = None,
    pool: asyncpg.Pool | None = None,
) -> int:
    """Insert or update locations for multiple websites in one batch.

    *rows* is a list of ``(website_id, lat, lng)`` tuples.
    Returns the number of rows updated.
    """
    if conn is not None:
        return await _bulk_set_locations(conn, rows)

    if pool is not None:
        async with pool.acquire() as c:
            return await _bulk_set_locations(c, rows)  # type: ignore[arg-type]

    raise ValueError("Either conn or pool must be provided")


async def _bulk_set_locations(
    conn: asyncpg.Connection,
    rows: list[tuple[int, float, float]],
) -> int:
    await conn.executemany(
        """
        UPDATE websites
        SET location = ST_SetSRID(ST_MakePoint($2, $1), 4326)
        WHERE id = $3
        """,
        [(lat, lng, website_id) for website_id, lat, lng in rows],
    )
    # asyncpg executemany() returns None (not a command tag), so we
    # return the batch size as the honest count.  Every tuple has a
    # WHERE id = ... clause; if the id exists the row is updated.
    return len(rows)
