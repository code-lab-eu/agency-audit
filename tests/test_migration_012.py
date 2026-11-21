"""Migration 012 clears the city viewports geocoded without a country filter."""

from __future__ import annotations

from pathlib import Path

import asyncpg

MIGRATION_012 = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agency_audit"
    / "migrations"
    / "012_reset_city_viewport.sql"
)


async def test_clears_cached_viewports(db_conn: asyncpg.Connection) -> None:
    """The viewport of Barcelona, geocoded to Espírito Santo in Brazil, is cleared."""
    city_id = await db_conn.fetchval(
        """INSERT INTO cities (country, label, slug, population,
                               viewport_low_lat, viewport_low_lng,
                               viewport_high_lat, viewport_high_lng)
           VALUES ('ES', 'Barcelona', 'barcelona-m012', 1620343,
                   -20.179187, -40.267432, -20.166501, -40.237717)
           RETURNING id"""
    )

    await db_conn.execute(MIGRATION_012.read_text())

    row = await db_conn.fetchrow(
        """SELECT viewport_low_lat, viewport_low_lng, viewport_high_lat, viewport_high_lng
             FROM cities WHERE id = $1""",
        city_id,
    )
    assert row is not None
    assert tuple(row) == (None, None, None, None)
