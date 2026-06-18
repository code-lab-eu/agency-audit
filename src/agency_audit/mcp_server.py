"""FastMCP server exposing discovery & audit tools for the agency-audit system.

Tools:
  get_next_city          — Returns next unprocessed city, marks as in_progress
  report_website         — Records a discovered website for a city
  get_unaudited_website  — Returns next pending website for audit
  submit_audit           — Stores audit results for a website
  get_stats              — Returns summary counts
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
from fastmcp import FastMCP

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="agency-audit",
    instructions=(
        "MCP server for the Real Estate Radar discovery & audit pipeline. "
        "Use get_next_city to claim a city for Google Maps research, "
        "report_website to submit discovered agencies, "
        "get_unaudited_website to claim a website for audit, "
        "submit_audit to store audit results, "
        "and get_stats for progress overview."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetchone(pool: asyncpg.Pool, query: str, *args: Any) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def _execute(pool: asyncpg.Pool, query: str, *args: Any) -> str:
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)


# ---------------------------------------------------------------------------
# Tool: get_next_city
# ---------------------------------------------------------------------------


@mcp.tool
async def get_next_city(country: str | None = None) -> dict[str, Any]:
    """Return the next unprocessed city for discovery and mark it as in_progress.

    Args:
        country: Optional ISO country code (e.g. "BG"). If omitted, the
            highest-population pending city from any country is returned.

    Returns:
        City dict with id, country, label, slug, population, latitude,
        longitude — or {"error": "no pending cities"} if none available.
    """
    pool = await get_pool()
    where = "discovery_status = 'pending'"
    params: list[Any] = []
    if country:
        where += " AND country = $1"
        params.append(country.upper())

    row = await _fetchone(
        pool,
        f"""
        SELECT id, country, label, slug, population, latitude, longitude
        FROM cities
        WHERE {where}
        ORDER BY population DESC
        LIMIT 1
        """,
        *params,
    )
    if row is None:
        return {"error": "no pending cities"}

    await _execute(
        pool,
        "UPDATE cities SET discovery_status = 'in_progress' WHERE id = $1",
        row["id"],
    )

    return dict(row)


# ---------------------------------------------------------------------------
# Tool: report_website
# ---------------------------------------------------------------------------


@mcp.tool
async def report_website(
    url: str,
    name: str,
    city: str,
    place_id: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    discovered_via: str = "google_maps",
) -> dict[str, Any]:
    """Report a discovered real estate agency website.

    Inserts the website (or links it if it already exists by URL) to the
    specified city and logs the discovery.

    Args:
        url:           Agency website URL.
        name:          Agency display name.
        city:          City slug (e.g. "sofia") or city ID as string.
        place_id:      Google Maps place ID.
        address:       Physical address of the agency.
        phone:         Phone number.
        discovered_via: How the website was discovered (default "google_maps").

    Returns:
        {"website_id": int, "city_id": int, "created": bool}
        where *created* is True if a new website row was inserted.
    """
    pool = await get_pool()

    # Resolve city
    async with pool.acquire() as conn:
        city_row = None
        if city.isdigit():
            city_row = await conn.fetchrow(
                "SELECT id FROM cities WHERE id = $1", int(city)
            )
        else:
            city_row = await conn.fetchrow(
                "SELECT id FROM cities WHERE slug = $1", city
            )
        if city_row is None:
            return {"error": f"city not found: {city}"}
        city_id = city_row["id"]

        # Insert or fetch website
        existing = await conn.fetchrow("SELECT id FROM websites WHERE url = $1", url)
        created = False
        if existing:
            website_id = existing["id"]
        else:
            website_id = await conn.fetchval(
                """
                INSERT INTO websites (url, label, maps_place_id, address, phone)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                url,
                name,
                place_id,
                address,
                phone,
            )
            created = True

        # Link website to city (ignore if already linked)
        await conn.execute(
            """
            INSERT INTO website_cities (website_id, city_id, discovered_via)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            website_id,
            city_id,
            discovered_via,
        )

        # Log discovery
        await conn.execute(
            """
            INSERT INTO discovery_log (city_id, website_id, agent, search_query, status)
            VALUES ($1, $2, $3, $4, 'found')
            """,
            city_id,
            website_id,
            discovered_via,
            f"{name} @ {url}",
        )

    return {"website_id": website_id, "city_id": city_id, "created": created}


# ---------------------------------------------------------------------------
# Tool: get_unaudited_website
# ---------------------------------------------------------------------------


@mcp.tool
async def get_unaudited_website() -> dict[str, Any]:
    """Return the next pending website for audit and mark it as 'auditing'.

    Returns:
        Website dict with id, url, label, maps_place_id, address, phone,
        city info — or {"error": "no pending websites"} if none available.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT w.id, w.url, w.label, w.maps_place_id, w.address, w.phone
            FROM websites w
            WHERE w.audit_status = 'pending'
            ORDER BY w.created_at
            LIMIT 1
            """,
        )
        if row is None:
            return {"error": "no pending websites"}

        await conn.execute(
            "UPDATE websites SET audit_status = 'auditing' WHERE id = $1",
            row["id"],
        )

        # Attach city info
        cities = await conn.fetch(
            """
            SELECT c.id, c.label, c.slug, c.country
            FROM website_cities wc
            JOIN cities c ON c.id = wc.city_id
            WHERE wc.website_id = $1
            """,
            row["id"],
        )

    result = dict(row)
    result["cities"] = [dict(c) for c in cities]
    return result


# ---------------------------------------------------------------------------
# Tool: submit_audit
# ---------------------------------------------------------------------------


@mcp.tool
async def submit_audit(
    website_id: int,
    robots_txt_ok: bool,
    anti_scraping_detected: bool,
    api_detected: bool,
    property_count: int,
    listing_quality_score: float,
    tech_stack: list[str] | None = None,
    overall_score: int = 0,
    notes: str | None = None,
) -> dict[str, Any]:
    """Submit audit results for a website.

    Stores structured audit_data in JSONB, sets audit_status to 'audited',
    and updates the score column.

    Args:
        website_id:            ID of the audited website.
        robots_txt_ok:         Whether robots.txt allows crawling.
        anti_scraping_detected: Whether anti-scraping measures were detected.
        api_detected:          Whether an API endpoint was found.
        property_count:        Number of property listings found.
        listing_quality_score: Quality score for listings (0.0–1.0).
        tech_stack:            List of detected technologies.
        overall_score:         Computed overall score (signed integer).
        notes:                 Free-text audit notes.

    Returns:
        {"website_id": int, "status": "audited"}
    """
    pool = await get_pool()

    audit_data = {
        "robots_txt_allows": robots_txt_ok,
        "has_anti_scraping": anti_scraping_detected,
        "has_api": api_detected,
        "property_count": property_count,
        "listing_quality_score": listing_quality_score,
        "technology_stack": tech_stack or [],
        "notes": notes,
    }

    result = await _execute(
        pool,
        """
        UPDATE websites
        SET audit_data = $1::jsonb,
            score = $2,
            audit_status = 'audited',
            last_audited_at = now()
        WHERE id = $3
        """,
        json.dumps(audit_data),
        overall_score,
        website_id,
    )

    if result == "UPDATE 0":
        return {"error": f"website {website_id} not found"}

    return {"website_id": website_id, "status": "audited"}


# ---------------------------------------------------------------------------
# Tool: get_stats
# ---------------------------------------------------------------------------


@mcp.tool
async def get_stats() -> dict[str, Any]:
    """Return summary statistics about the discovery & audit pipeline.

    Returns:
        Dict with countries_processed, cities_processed, cities_in_progress,
        cities_pending, websites_discovered, websites_audited,
        websites_pending, average_score.
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        countries_processed = await conn.fetchval(
            "SELECT COUNT(DISTINCT country) FROM cities WHERE discovery_status = 'done'"
        )
        cities_processed = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        cities_in_progress = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'in_progress'"
        )
        cities_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'pending'"
        )
        websites_discovered = await conn.fetchval("SELECT COUNT(*) FROM websites")
        websites_audited = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'audited'"
        )
        websites_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'pending'"
        )
        avg_score = await conn.fetchval(
            "SELECT COALESCE(AVG(score), 0) FROM websites WHERE audit_status = 'audited'"
        )

    return {
        "countries_processed": countries_processed,
        "cities_processed": cities_processed,
        "cities_in_progress": cities_in_progress,
        "cities_pending": cities_pending,
        "websites_discovered": websites_discovered,
        "websites_audited": websites_audited,
        "websites_pending": websites_pending,
        "average_score": round(float(avg_score), 2),
    }


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for running the MCP server via stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
