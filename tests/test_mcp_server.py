"""Tests for the agency-audit MCP server tools.

All tests run against a live PostgreSQL database.

Each test receives a private, pristine database via the ``fresh_db`` fixture
(conftest.py).  ``fresh_db`` clones a schema-only session template, redirects
``get_pool()`` onto the private database, seeds it with the canonical reference
data (44 countries + 20 BG cities), and drops the database on teardown.  No
manual cleanup is required, and exact-count assertions are safe because the
database is pristine.

The MCP tools (``get_next_city``, ``get_unaudited_website``, ``report_website``,
``submit_audit``, ``get_stats``) all open their own connections via
``get_pool()``, so the transaction-rollback isolation of the shared ``db_conn``
fixture cannot reach them — ``fresh_db`` is the correct fit for this module.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg

from agency_audit.mcp_server import (
    get_next_city,
    get_stats,
    get_unaudited_website,
    report_website,
    submit_audit,
)

# ---------------------------------------------------------------------------
# get_next_city
# ---------------------------------------------------------------------------


async def test_get_next_city_returns_pending_city(fresh_db: asyncpg.Connection) -> None:
    result = await get_next_city()
    assert "error" not in result
    assert "id" in result
    assert "country" in result
    assert "label" in result
    assert "slug" in result
    assert "population" in result


async def test_get_next_city_marks_in_progress(fresh_db: asyncpg.Connection) -> None:
    result = await get_next_city()
    city_id = result["id"]
    status = await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE id = $1", city_id)
    assert status == "in_progress"


async def test_get_next_city_by_country(fresh_db: asyncpg.Connection) -> None:
    # Bulgaria has cities seeded from geonames
    result = await get_next_city(country="BG")
    assert "error" not in result
    assert result["country"] == "BG"


async def test_get_next_city_unknown_country(fresh_db: asyncpg.Connection) -> None:
    result = await get_next_city(country="ZZ")
    assert "error" in result


async def test_get_next_city_highest_population_first(fresh_db: asyncpg.Connection) -> None:
    """City with highest population should be returned first."""
    # Get the max population city from pending cities BEFORE calling get_next_city
    max_pop = await fresh_db.fetchval(
        "SELECT MAX(population) FROM cities WHERE discovery_status = 'pending'"
    )
    result = await get_next_city()
    assert result["population"] == max_pop


# ---------------------------------------------------------------------------
# report_website
# ---------------------------------------------------------------------------


async def test_report_website_creates_new(fresh_db: asyncpg.Connection) -> None:
    result = await report_website(
        url="https://test-agency.example.com",
        name="Test Agency",
        city="sofia",
        place_id="ChIJ1234",
        address="123 Test St",
        phone="+359 2 123 4567",
    )
    assert result["created"] is True
    assert "website_id" in result
    assert "city_id" in result

    # Verify website was inserted
    row = await fresh_db.fetchrow(
        "SELECT url, label, maps_place_id, address, phone FROM websites WHERE id = $1",
        result["website_id"],
    )
    assert row["url"] == "https://test-agency.example.com"
    assert row["label"] == "Test Agency"
    assert row["maps_place_id"] == "ChIJ1234"
    assert row["address"] == "123 Test St"
    assert row["phone"] == "+359 2 123 4567"

    # Verify website_cities link
    link = await fresh_db.fetchrow(
        "SELECT * FROM website_cities WHERE website_id = $1 AND city_id = $2",
        result["website_id"],
        result["city_id"],
    )
    assert link is not None


async def test_report_website_idempotent_url(fresh_db: asyncpg.Connection) -> None:
    """Reporting the same URL twice should not create a duplicate website."""
    r1 = await report_website(
        url="https://test-dup.example.com",
        name="Dup Agency",
        city="sofia",
    )
    r2 = await report_website(
        url="https://test-dup.example.com",
        name="Dup Agency",
        city="sofia",
    )
    assert r1["website_id"] == r2["website_id"]
    assert r1["created"] is True
    assert r2["created"] is False


async def test_report_website_unknown_city(fresh_db: asyncpg.Connection) -> None:
    result = await report_website(
        url="https://test-unknown.example.com",
        name="Unknown City Agency",
        city="nonexistent-city-slug",
    )
    assert "error" in result


async def test_report_website_city_by_id(fresh_db: asyncpg.Connection) -> None:
    """Report website using numeric city ID."""
    city_id = await fresh_db.fetchval("SELECT id FROM cities LIMIT 1")
    result = await report_website(
        url="https://test-byid.example.com",
        name="ByID Agency",
        city=str(city_id),
    )
    assert result["city_id"] == city_id


# ---------------------------------------------------------------------------
# get_unaudited_website
# ---------------------------------------------------------------------------


async def test_get_unaudited_website_returns_pending(fresh_db: asyncpg.Connection) -> None:
    # First report a website (must be visible to the pool's connections)
    await report_website(
        url="https://test-unaudited.example.com",
        name="Unaudited Agency",
        city="sofia",
    )
    result = await get_unaudited_website()
    assert "error" not in result
    assert "id" in result
    assert "url" in result
    assert "cities" in result
    assert result["url"] == "https://test-unaudited.example.com"

    # Verify it was marked as auditing
    status = await fresh_db.fetchval(
        "SELECT audit_status FROM websites WHERE id = $1", result["id"]
    )
    assert status == "auditing"


async def test_get_unaudited_website_none_pending(fresh_db: asyncpg.Connection) -> None:
    """When no pending websites exist, return error.

    The pristine database has zero websites, so there are no unaudited
    ones — the tool must return the expected error immediately.
    """
    result = await get_unaudited_website()
    assert result == {"error": "no pending websites"}


# ---------------------------------------------------------------------------
# submit_audit
# ---------------------------------------------------------------------------


async def test_submit_audit_stores_results(fresh_db: asyncpg.Connection) -> None:
    # Create a website to audit
    ws = await report_website(
        url="https://test-audit.example.com",
        name="Audit Test Agency",
        city="sofia",
    )
    website_id = ws["website_id"]

    result = await submit_audit(
        website_id=website_id,
        robots_txt_ok=True,
        anti_scraping_detected=False,
        api_detected=True,
        property_count=500,
        listing_quality_score=0.8,
        tech_stack=["WordPress", "Elementor"],
        overall_score=65,
        notes="Good site, clean structure",
    )
    assert result["status"] == "audited"
    assert result["website_id"] == website_id

    # Verify stored data
    row = await fresh_db.fetchrow(
        "SELECT audit_data, score, audit_status, last_audited_at FROM websites WHERE id = $1",
        website_id,
    )
    assert row["audit_status"] == "audited"
    assert row["score"] == 65
    assert row["last_audited_at"] is not None

    audit = json.loads(row["audit_data"])
    assert audit["robots_txt_allows"] is True
    assert audit["has_anti_scraping"] is False
    assert audit["has_api"] is True
    assert audit["property_count"] == 500
    assert audit["listing_quality_score"] == 0.8
    assert audit["technology_stack"] == ["WordPress", "Elementor"]
    assert audit["notes"] == "Good site, clean structure"


async def test_submit_audit_nonexistent_website(fresh_db: asyncpg.Connection) -> None:
    result = await submit_audit(
        website_id=999999,
        robots_txt_ok=True,
        anti_scraping_detected=False,
        api_detected=False,
        property_count=0,
        listing_quality_score=0.0,
        overall_score=0,
    )
    assert "error" in result


async def test_submit_audit_negative_score(fresh_db: asyncpg.Connection) -> None:
    """Score can be negative for unsuitable sites."""
    ws = await report_website(
        url="https://test-negative.example.com",
        name="Bad Site Agency",
        city="sofia",
    )
    result = await submit_audit(
        website_id=ws["website_id"],
        robots_txt_ok=False,
        anti_scraping_detected=True,
        api_detected=False,
        property_count=0,
        listing_quality_score=0.0,
        overall_score=-50,
    )
    assert result["status"] == "audited"

    score = await fresh_db.fetchval("SELECT score FROM websites WHERE id = $1", ws["website_id"])
    assert score == -50


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


async def test_get_stats_returns_all_fields(fresh_db: asyncpg.Connection) -> None:
    """Stats on a pristine seed: 0 countries processed, 20 pending cities, 0 websites."""
    result = await get_stats()
    assert "countries_processed" in result
    assert "cities_processed" in result
    assert "cities_in_progress" in result
    assert "cities_pending" in result
    assert "websites_discovered" in result
    assert "websites_audited" in result
    assert "websites_pending" in result
    assert "average_score" in result
    assert result["countries_processed"] == 0
    assert result["cities_processed"] == 0
    assert result["cities_in_progress"] == 0
    assert result["cities_pending"] == 20
    assert result["websites_discovered"] == 0
    assert result["websites_audited"] == 0
    assert result["websites_pending"] == 0
    assert result["average_score"] == 0.0


async def test_get_stats_reflects_audit(fresh_db: asyncpg.Connection) -> None:
    # Report and audit a website
    ws = await report_website(
        url="https://test-stats.example.com",
        name="Stats Agency",
        city="sofia",
    )
    await submit_audit(
        website_id=ws["website_id"],
        robots_txt_ok=True,
        anti_scraping_detected=False,
        api_detected=True,
        property_count=100,
        listing_quality_score=0.9,
        overall_score=42,
    )

    stats = await get_stats()
    assert stats["websites_discovered"] == 1
    assert stats["websites_audited"] == 1
    assert stats["average_score"] == 42.0


# ---------------------------------------------------------------------------
# Full pipeline integration test
# ---------------------------------------------------------------------------


async def test_full_pipeline(fresh_db: asyncpg.Connection) -> None:
    """Test the full discovery → audit pipeline flow."""
    # 1. Get next city
    city = await get_next_city(country="BG")
    assert "error" not in city

    # 2. Report a website for that city
    city_slug = city["slug"]
    ws = await report_website(
        url="https://test-pipeline.example.com",
        name="Pipeline Agency",
        city=city_slug,
        place_id="ChIJpipeline",
    )
    assert ws["created"] is True

    # 3. Get unaudited website
    unaudited = await get_unaudited_website()
    assert unaudited["url"] == "https://test-pipeline.example.com"

    # 4. Submit audit
    audit = await submit_audit(
        website_id=unaudited["id"],
        robots_txt_ok=True,
        anti_scraping_detected=False,
        api_detected=True,
        property_count=250,
        listing_quality_score=0.75,
        tech_stack=["React", "Node.js"],
        overall_score=55,
    )
    assert audit["status"] == "audited"

    # 5. Check stats reflect the work
    stats = await get_stats()
    assert stats["websites_discovered"] == 1
    assert stats["websites_audited"] == 1


# ---------------------------------------------------------------------------
# Atomic claim tests (real DB — concurrent FOR UPDATE SKIP LOCKED)
# ---------------------------------------------------------------------------


async def test_get_next_city_skips_locked_row(fresh_db: asyncpg.Connection) -> None:
    """FOR UPDATE SKIP LOCKED skips a row locked by another transaction.

    Locks the highest-population pending city in a separate transaction
    via ``fresh_db`` without changing its ``discovery_status``.  A
    concurrent ``get_next_city()`` opens its own connection where
    ``FOR UPDATE SKIP LOCKED`` must skip the locked row and pick the
    next-highest city — returning promptly instead of blocking.
    Without SKIP LOCKED this test would time out.
    """
    top_two = await fresh_db.fetch(
        "SELECT id, population FROM cities "
        "WHERE discovery_status = 'pending' "
        "ORDER BY population DESC LIMIT 2"
    )
    assert len(top_two) == 2
    top_id = top_two[0]["id"]
    top_pop = top_two[0]["population"]
    second_id = top_two[1]["id"]

    async with fresh_db.transaction():
        await fresh_db.execute("SELECT id FROM cities WHERE id = $1 FOR UPDATE", top_id)
        # top_id is now row-locked; get_next_city() must skip it.
        result = await asyncio.wait_for(get_next_city(), timeout=5.0)
        assert result["id"] == second_id, (
            f"Expected city {second_id} (skipped locked row), got {result['id']}"
        )
        assert result["population"] != top_pop, (
            f"Got locked-row population {top_pop} — SKIP LOCKED failed"
        )


async def test_get_next_city_no_pending_cities(fresh_db: asyncpg.Connection) -> None:
    """When no pending cities exist, get_next_city returns an error.

    With fresh_db each test owns a private database that is dropped on
    teardown, so we can safely mark every city as done without snapshotting
    or restoring — no other test can see the mutation.
    """
    await fresh_db.execute("UPDATE cities SET discovery_status = 'done'")
    result = await get_next_city()
    assert result == {"error": "no pending cities"}


async def test_get_unaudited_website_skips_locked_row(
    fresh_db: asyncpg.Connection,
) -> None:
    """FOR UPDATE SKIP LOCKED skips a website row locked by another transaction.

    Seeds two pending websites via ``report_website()`` (committed, visible
    to all connections), then locks the earlier-created one in a separate
    ``fresh_db`` transaction.  ``get_unaudited_website()`` opens its own
    connection where ``FOR UPDATE SKIP LOCKED`` must skip the locked row
    and return the other website — promptly, without blocking.
    """
    # Seed two pending websites.
    a = await report_website(url="https://test-claim-a.example.com", name="Claim A", city="sofia")
    b = await report_website(url="https://test-claim-b.example.com", name="Claim B", city="sofia")
    assert a["website_id"] != b["website_id"]

    # Lock the earlier-created website (ORDER BY created_at would pick it first).
    async with fresh_db.transaction():
        await fresh_db.execute("SELECT id FROM websites WHERE id = $1 FOR UPDATE", a["website_id"])
        result = await asyncio.wait_for(get_unaudited_website(), timeout=5.0)
        assert result["id"] == b["website_id"], (
            f"Expected website {b['website_id']} (skipped locked row), got {result['id']}"
        )
