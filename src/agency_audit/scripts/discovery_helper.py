"""
Discovery pipeline helper — inserts agencies into DB and manages city states.
Run: source .venv/bin/activate && python /path/to/this_script.py [command] [args]

Commands:
  insert <city_id> <name> <url> <phone> <address>  — insert a single agency
  batch <city_id> <search_query> --json '<json_array>'  — batch insert agencies
  mark-done <city_id>  — mark city as done
  mark-failed <city_id> <reason>  — mark city as failed
  pending <country_iso> <limit>  — list pending cities for a country
"""

import asyncio
import json
import os
import sys

os.chdir("/opt/data/workspace/agency-audit")
sys.path.insert(0, "src")
from agency_audit.db import get_pool  # noqa: E402  (import after sys.path setup)


async def insert_agency(
    city_id: int,
    name: str,
    url: str,
    phone: str | None = None,
    address: str | None = None,
    place_id: str | None = None,
    search_query: str | None = None,
):
    """Insert a single agency into the database."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check if this URL already exists
        existing = await conn.fetchval("SELECT id FROM websites WHERE url = $1", url)
        if existing:
            website_id = existing
            created = False
        else:
            website_id = await conn.fetchval(
                """INSERT INTO websites (url, label, maps_place_id, address, phone)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (url) DO UPDATE SET label = EXCLUDED.label
                   RETURNING id""",
                url,
                name,
                place_id,
                address,
                phone,
            )
            created = True

        # Link to city
        await conn.execute(
            """INSERT INTO website_cities (website_id, city_id, discovered_via)
               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
            website_id,
            city_id,
            "google_maps_browser",
        )

        # Log discovery
        q = search_query or f"{name} @ {url}"
        await conn.execute(
            """INSERT INTO discovery_log (city_id, website_id, agent, search_query, status)
               VALUES ($1, $2, $3, $4, 'found')""",
            city_id,
            website_id,
            "google_maps_browser",
            q,
        )
        return website_id, created


async def batch_insert(city_id: int, search_query: str, agencies: list[dict]):
    """Batch insert agencies from a list."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = 0
        for a in agencies:
            name = a.get("name", "").strip()
            url = a.get("website", "")
            phone = a.get("phone", "")
            address = a.get("address", "")
            place_id = a.get("place_id", "")

            if not name or not url:
                # Skip entries without website URL, use maps URL as fallback
                if not url and place_id:
                    url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"
                if not url and name:
                    query = f"{name.replace(' ', '+')}+{address.replace(' ', '+')}"
                    url = f"https://www.google.com/search?q={query}".strip()
                if not url:
                    continue

            existing = await conn.fetchval("SELECT id FROM websites WHERE url = $1", url)
            if existing:
                website_id = existing
            else:
                website_id = await conn.fetchval(
                    """INSERT INTO websites (url, label, maps_place_id, address, phone)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (url) DO UPDATE SET label = EXCLUDED.label
                       RETURNING id""",
                    url,
                    name,
                    place_id or None,
                    address or None,
                    phone or None,
                )

            await conn.execute(
                """INSERT INTO website_cities (website_id, city_id, discovered_via)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                website_id,
                city_id,
                "google_maps_browser",
            )

            log_query = f"{name} @ {url}"
            await conn.execute(
                """INSERT INTO discovery_log (city_id, website_id, agent, search_query, status)
                   VALUES ($1, $2, $3, $4, 'found')""",
                city_id,
                website_id,
                "google_maps_browser",
                log_query,
            )
            count += 1

        return count


async def mark_city(city_id: int, status: str, note: str | None = None):
    """Mark a city's discovery status."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cities SET discovery_status = $1 WHERE id = $2",
            status,
            city_id,
        )
        if note:
            log_status = "searched"  # valid enum values: searched, found, skipped, failed
            await conn.execute(
                """INSERT INTO discovery_log (city_id, agent, search_query, status)
                   VALUES ($1, $2, $3, $4)""",
                city_id,
                "google_maps_browser",
                note,
                log_status,
            )


async def list_pending(country_iso: str | None = None, limit: int = 5):
    """List pending cities for a country, or the top pending cities across all countries."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if country_iso:
            rows = await conn.fetch(
                """SELECT id, label, country, population, latitude, longitude
                   FROM cities
                   WHERE discovery_status = 'pending' AND country = $1
                   ORDER BY population DESC LIMIT $2""",
                country_iso,
                limit,
            )
        else:
            rows = await conn.fetch(
                """SELECT id, label, country, population, latitude, longitude
                   FROM cities
                   WHERE discovery_status = 'pending'
                   ORDER BY population DESC LIMIT $2""",
                limit,
            )
        return [dict(r) for r in rows]


async def main():
    if len(sys.argv) < 2:
        print("Usage: python discovery_helper.py [command] [args...]")
        print("Commands:")
        print("  list-pending [country_iso] [limit=5]")
        print("  batch <city_id> <search_query> --json '<json>'")
        print("  mark-done <city_id>")
        print("  mark-failed <city_id> <reason>")
        return

    cmd = sys.argv[1]

    if cmd == "list-pending":
        country = sys.argv[2] if len(sys.argv) > 2 else None
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        pending = await list_pending(country, limit)
        print(json.dumps(pending, default=str))

    elif cmd == "batch":
        city_id = int(sys.argv[2])
        search_query = sys.argv[3]
        json_str = sys.argv[4] if len(sys.argv) > 4 else "[]"
        agencies = json.loads(json_str)
        count = await batch_insert(city_id, search_query, agencies)
        print(f"Inserted {count} agencies for city {city_id}")
        # Mark as done
        await mark_city(city_id, "done", search_query)
        print(f"City {city_id} marked as done")

    elif cmd == "mark-done":
        city_id = int(sys.argv[2])
        await mark_city(city_id, "done", "manually completed")
        print(f"City {city_id} marked done")

    elif cmd == "mark-failed":
        city_id = int(sys.argv[2])
        reason = sys.argv[3] if len(sys.argv) > 3 else "unknown"
        await mark_city(city_id, "failed", reason)
        print(f"City {city_id} marked failed: {reason}")

    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    asyncio.run(main())
