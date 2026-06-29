"""Viewport preset storage layer.

Provides async CRUD operations for the ``viewport_presets`` table.
Independent module — does not depend on search, geometry, or audit modules.
Uses its own DB access patterns via ``get_pool()``.
"""

from __future__ import annotations

import logging
from typing import Any

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)


async def save_viewport(data: dict[str, Any]) -> int:
    """Insert a new viewport preset and return its id.

    Required keys: ``name``, ``center_lat``, ``center_lng``, ``zoom_level``,
    ``north``, ``south``, ``east``, ``west``.  Optional: ``user_id``.

    Returns the newly-inserted row id.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            """INSERT INTO viewport_presets
               (user_id, name, center_lat, center_lng, zoom_level,
                north, south, east, west)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               RETURNING id""",
            data.get("user_id"),
            data["name"],
            data["center_lat"],
            data["center_lng"],
            data["zoom_level"],
            data["north"],
            data["south"],
            data["east"],
            data["west"],
        )
    logger.info("Saved viewport preset %d: %s", row_id, data.get("name"))
    return int(row_id)


async def load_viewports(user_id: str) -> list[dict[str, Any]]:
    """Load all viewport presets for a given user, newest first.

    Returns a list of dicts with all columns (id, user_id, name, center_lat,
    center_lng, zoom_level, north, south, east, west, created_at, updated_at).
    Timestamps are ISO-format strings.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, user_id, name, center_lat, center_lng, zoom_level,
                      north, south, east, west, created_at, updated_at
               FROM viewport_presets
               WHERE user_id = $1
               ORDER BY created_at DESC""",
            user_id,
        )

    return [
        {
            "id": r["id"],
            "user_id": r["user_id"],
            "name": r["name"],
            "center_lat": float(r["center_lat"]),
            "center_lng": float(r["center_lng"]),
            "zoom_level": r["zoom_level"],
            "north": float(r["north"]),
            "south": float(r["south"]),
            "east": float(r["east"]),
            "west": float(r["west"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


async def delete_viewport(preset_id: int) -> bool:
    """Delete a viewport preset by id.

    Returns ``True`` if a row was deleted, ``False`` if the id did not exist.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM viewport_presets WHERE id = $1",
            preset_id,
        )

    # asyncpg's Connection.execute returns a string like "DELETE 1"
    deleted = result != "DELETE 0"
    if deleted:
        logger.info("Deleted viewport preset %d", preset_id)
    return deleted
