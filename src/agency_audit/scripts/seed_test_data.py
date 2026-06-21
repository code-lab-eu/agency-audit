"""Insert test data into the database for dashboard testing."""

import asyncio
import json

import asyncpg

from agency_audit.config import settings


async def main():
    conn = await asyncpg.connect(dsn=settings.dsn)

    websites = [
        (
            "https://example-agency1.bg",
            "Sofia Properties Ltd",
            75,
            "audited",
            {
                "robots_txt_allows": True,
                "has_anti_scraping": False,
                "has_api": True,
                "property_count": 1250,
                "has_property_map": True,
                "listings_have_price": True,
                "listings_have_location": True,
                "listings_have_images": True,
                "technology_stack": ["WordPress", "PHP"],
                "response_time_ms": 340,
                "ssl_valid": True,
                "language": "bg",
                "listing_quality_score": 0.85,
                "notes": "Standard WordPress real estate plugin",
            },
        ),
        (
            "https://example-agency2.bg",
            "Plovdiv Estates",
            55,
            "audited",
            {
                "robots_txt_allows": True,
                "has_anti_scraping": False,
                "has_api": False,
                "property_count": 530,
                "has_property_map": True,
                "listings_have_price": True,
                "listings_have_location": True,
                "listings_have_images": True,
                "technology_stack": ["Drupal"],
                "response_time_ms": 450,
                "ssl_valid": True,
                "language": "bg",
                "listing_quality_score": 0.7,
                "notes": "Drupal-based site",
            },
        ),
        (
            "https://example-agency3.bg",
            "Varna Real Estate",
            30,
            "audited",
            {
                "robots_txt_allows": True,
                "has_anti_scraping": False,
                "has_api": False,
                "property_count": 120,
                "has_property_map": False,
                "listings_have_price": True,
                "listings_have_location": True,
                "listings_have_images": False,
                "technology_stack": ["Joomla"],
                "response_time_ms": 800,
                "ssl_valid": True,
                "language": "bg",
                "listing_quality_score": 0.5,
                "notes": "Joomla site",
            },
        ),
        (
            "https://bad-agency.bg",
            "Blocked Site",
            -20,
            "audited",
            {
                "robots_txt_allows": False,
                "has_anti_scraping": True,
                "has_api": False,
                "property_count": 0,
                "has_property_map": False,
                "listings_have_price": False,
                "listings_have_location": False,
                "listings_have_images": False,
                "technology_stack": [],
                "response_time_ms": 2000,
                "ssl_valid": False,
                "language": "bg",
                "listing_quality_score": 0.0,
                "notes": "Cloudflare blocked, robots.txt disallows",
            },
        ),
        ("https://pending-agency.bg", "Pending Agency", 0, "pending", {}),
    ]

    for url, label, score, status, audit_data in websites:
        existing = await conn.fetchval("SELECT id FROM websites WHERE url = $1", url)
        if existing:
            await conn.execute(
                "UPDATE websites SET score=$1, audit_status=$2, audit_data=$3::jsonb, "
                "last_audited_at=now() WHERE id=$4",
                score,
                status,
                json.dumps(audit_data),
                existing,
            )
            wid = existing
        else:
            wid = await conn.fetchval(
                "INSERT INTO websites (url, label, score, audit_status, audit_data, "
                "last_audited_at) VALUES ($1, $2, $3, $4, $5::jsonb, now()) RETURNING id",
                url,
                label,
                score,
                status,
                json.dumps(audit_data),
            )

        # Link to Sofia (id=6) and Plovdiv (id=10) for some
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id, discovered_via) "
            "VALUES ($1, 6, 'google_maps') ON CONFLICT DO NOTHING",
            wid,
        )
        if "Plovdiv" in label or "Varna" in label:
            city_id = 10 if "Plovdiv" in label else 3
            await conn.execute(
                "INSERT INTO website_cities (website_id, city_id, discovered_via) "
                "VALUES ($1, $2, 'google_maps') ON CONFLICT DO NOTHING",
                wid,
                city_id,
            )

    # Mark some cities as done/in_progress
    await conn.execute("UPDATE cities SET discovery_status = 'done' WHERE id IN (6, 10, 3)")
    await conn.execute("UPDATE cities SET discovery_status = 'in_progress' WHERE id IN (15, 5)")

    count = await conn.fetchval("SELECT COUNT(*) FROM websites")
    print(f"Total websites: {count}")

    rows = await conn.fetch(
        "SELECT id, url, label, score, audit_status FROM websites ORDER BY score DESC"
    )
    for r in rows:
        print(f"  {r['id']}: {r['label']} score={r['score']} status={r['audit_status']}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
