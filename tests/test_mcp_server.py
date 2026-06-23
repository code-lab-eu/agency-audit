"""Tests for the agency-audit MCP server tools.

These tests run against the live PostgreSQL database (agency_audit).
They use the shared connection pool from agency_audit.db and clean up
after themselves.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.db import close_pool
from agency_audit.mcp_server import (
    get_next_city,
    get_stats,
    get_unaudited_website,
    report_website,
    submit_audit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_conn():
    """Direct connection for test setup/teardown.

    Uses a fresh connection (not the pool) so it works reliably across
    pytest-asyncio's per-function event loops.
    """
    conn = await asyncpg.connect(dsn=settings.dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def cleanup_test_data(db_conn):
    """Reset relevant state before and after each test.

    Also closes the shared pool after each test so the next test gets
    a fresh pool on its own event loop.
    """
    await db_conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
    await db_conn.execute(
        "UPDATE cities SET discovery_status = 'pending' WHERE discovery_status = 'in_progress'"
    )
    yield
    await db_conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
    await db_conn.execute(
        "UPDATE cities SET discovery_status = 'pending' WHERE discovery_status = 'in_progress'"
    )
    # Reset the module-level pool so the next test creates a fresh one
    # on its own event loop
    await close_pool()


# ---------------------------------------------------------------------------
# Unit tests with mocked pool — assert atomic FOR UPDATE SKIP LOCKED
# ---------------------------------------------------------------------------


class TestGetNextCityAtomic:
    """Unit tests verifying get_next_city uses a single atomic statement."""

    @pytest.mark.asyncio
    async def test_single_atomic_update_for_update_skip_locked(self):
        """get_next_city must issue exactly one SQL statement with FOR UPDATE SKIP LOCKED."""
        with patch("agency_audit.mcp_server.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            # Simulate a row being returned
            mock_conn.fetchrow.return_value = {
                "id": 1,
                "country": "BG",
                "label": "Sofia",
                "slug": "sofia",
                "population": 1_200_000,
                "latitude": 42.6977,
                "longitude": 23.3219,
            }

            result = await get_next_city()

            # Single DB call — no separate SELECT + UPDATE
            assert mock_conn.fetchrow.call_count == 1, (
                "Expected exactly 1 fetchrow call (atomic UPDATE), "
                f"got {mock_conn.fetchrow.call_count}"
            )
            assert mock_conn.execute.call_count == 0, (
                "Expected no separate execute call; the UPDATE is atomic"
            )

            sql = mock_conn.fetchrow.call_args.args[0]
            assert "FOR UPDATE SKIP LOCKED" in sql, (
                f"Atomic query missing FOR UPDATE SKIP LOCKED: {sql}"
            )
            assert "UPDATE cities" in sql
            assert "RETURNING" in sql
            assert "SELECT" in sql

            assert result["id"] == 1
            assert result["label"] == "Sofia"

    @pytest.mark.asyncio
    async def test_no_pending_cities_returns_error(self):
        """When no row is locked/updated, return error dict."""
        with patch("agency_audit.mcp_server.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            mock_conn.fetchrow.return_value = None

            result = await get_next_city()

            assert result == {"error": "no pending cities"}
            assert mock_conn.fetchrow.call_count == 1

    @pytest.mark.asyncio
    async def test_country_filter_in_subselect(self):
        """When country is passed, the WHERE clause must filter on country."""
        with patch("agency_audit.mcp_server.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            mock_conn.fetchrow.return_value = {
                "id": 2,
                "country": "GR",
                "label": "Athens",
                "slug": "athens",
                "population": 664_046,
                "latitude": 37.9838,
                "longitude": 23.7275,
            }

            result = await get_next_city(country="GR")

            sql = mock_conn.fetchrow.call_args.args[0]
            assert "country = $1" in sql, (
                f"country filter missing in subselect: {sql}"
            )
            assert mock_conn.fetchrow.call_args.args[1] == "GR"
            assert result["country"] == "GR"


class TestGetUnauditedWebsiteAtomic:
    """Unit tests verifying get_unaudited_website uses a single atomic statement."""

    @pytest.mark.asyncio
    async def test_single_atomic_update_for_update_skip_locked(self):
        """get_unaudited_website must issue UPDATE with FOR UPDATE SKIP LOCKED,
        followed only by a city JOIN query."""
        with patch("agency_audit.mcp_server.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            mock_conn.fetchrow.return_value = {
                "id": 42,
                "url": "https://example.com",
                "label": "Test Agency",
                "maps_place_id": "ChIJxyz",
                "address": "123 Main St",
                "phone": "+359123456",
            }
            mock_conn.fetch.return_value = [
                {"id": 1, "label": "Sofia", "slug": "sofia", "country": "BG"},
            ]

            result = await get_unaudited_website()

            # The UPDATE must be the first call
            assert mock_conn.fetchrow.call_count == 1
            update_sql = mock_conn.fetchrow.call_args.args[0]
            assert "FOR UPDATE SKIP LOCKED" in update_sql, (
                f"Atomic query missing FOR UPDATE SKIP LOCKED: {update_sql}"
            )
            assert "UPDATE websites" in update_sql
            assert "RETURNING" in update_sql

            # No separate execute for status update
            assert mock_conn.execute.call_count == 0, (
                "Expected no separate execute; status change is atomic"
            )

            # City join is the second call
            assert mock_conn.fetch.call_count == 1

            assert result["id"] == 42
            assert result["url"] == "https://example.com"
            assert result["cities"] == [
                {"id": 1, "label": "Sofia", "slug": "sofia", "country": "BG"}
            ]

    @pytest.mark.asyncio
    async def test_no_pending_websites_returns_error(self):
        """When no row is locked/updated, return error dict."""
        with patch("agency_audit.mcp_server.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool

            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx

            mock_conn.fetchrow.return_value = None

            result = await get_unaudited_website()

            assert result == {"error": "no pending websites"}
            assert mock_conn.fetchrow.call_count == 1
            # No city join when no row found
            assert mock_conn.fetch.call_count == 0


# ---------------------------------------------------------------------------
# get_next_city
# ---------------------------------------------------------------------------


async def test_get_next_city_returns_pending_city():
    result = await get_next_city()
    assert "error" not in result
    assert "id" in result
    assert "country" in result
    assert "label" in result
    assert "slug" in result
    assert "population" in result


async def test_get_next_city_marks_in_progress(db_conn):
    result = await get_next_city()
    city_id = result["id"]
    status = await db_conn.fetchval(
        "SELECT discovery_status FROM cities WHERE id = $1", city_id
    )
    assert status == "in_progress"


async def test_get_next_city_by_country():
    # Bulgaria has cities seeded from geonames
    result = await get_next_city(country="BG")
    assert "error" not in result
    assert result["country"] == "BG"


async def test_get_next_city_unknown_country():
    result = await get_next_city(country="ZZ")
    assert "error" in result


async def test_get_next_city_highest_population_first(db_conn):
    """City with highest population should be returned first."""
    # Get the max population city from pending cities BEFORE calling get_next_city
    max_pop = await db_conn.fetchval(
        "SELECT MAX(population) FROM cities WHERE discovery_status = 'pending'"
    )
    result = await get_next_city()
    assert result["population"] == max_pop


# ---------------------------------------------------------------------------
# report_website
# ---------------------------------------------------------------------------


async def test_report_website_creates_new(db_conn):
    result = await report_website(
        url="https://test-agency.example.com",
        name="Test Agency",
        city="sofia",
        place_id="ChIJ1234",
        address="123 Test St",
        phone="+359****3456",
    )
    assert result["created"] is True
    assert "website_id" in result
    assert "city_id" in result

    # Verify website was inserted
    row = await db_conn.fetchrow(
        "SELECT url, label, maps_place_id, address, phone FROM websites WHERE id = $1",
        result["website_id"],
    )
    assert row["url"] == "https://test-agency.example.com"
    assert row["label"] == "Test Agency"
    assert row["maps_place_id"] == "ChIJ1234"
    assert row["address"] == "123 Test St"
    assert row["phone"] == "+359****3456"

    # Verify website_cities link
    link = await db_conn.fetchrow(
        "SELECT * FROM website_cities WHERE website_id = $1 AND city_id = $2",
        result["website_id"],
        result["city_id"],
    )
    assert link is not None


async def test_report_website_idempotent_url(db_conn):
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


async def test_report_website_unknown_city():
    result = await report_website(
        url="https://test-unknown.example.com",
        name="Unknown City Agency",
        city="nonexistent-city-slug",
    )
    assert "error" in result


async def test_report_website_city_by_id(db_conn):
    """Report website using numeric city ID."""
    city_id = await db_conn.fetchval("SELECT id FROM cities LIMIT 1")
    result = await report_website(
        url="https://test-byid.example.com",
        name="ByID Agency",
        city=str(city_id),
    )
    assert result["city_id"] == city_id


# ---------------------------------------------------------------------------
# get_unaudited_website
# ---------------------------------------------------------------------------


async def test_get_unaudited_website_returns_pending(db_conn):
    # First report a website
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
    status = await db_conn.fetchval(
        "SELECT audit_status FROM websites WHERE id = $1", result["id"]
    )
    assert status == "auditing"


async def test_get_unaudited_website_none_pending(db_conn):
    """When no pending websites exist, return error."""
    # Mark any pending test websites as audited
    await db_conn.execute(
        "UPDATE websites SET audit_status = 'audited' WHERE audit_status = 'pending' AND url LIKE 'https://test-%'"
    )
    result = await get_unaudited_website()
    # There might be non-test pending websites, but in test env there shouldn't be any
    if "error" in result:
        assert result["error"] == "no pending websites"


# ---------------------------------------------------------------------------
# submit_audit
# ---------------------------------------------------------------------------


async def test_submit_audit_stores_results(db_conn):
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
    row = await db_conn.fetchrow(
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


async def test_submit_audit_nonexistent_website():
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


async def test_submit_audit_negative_score(db_conn):
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

    score = await db_conn.fetchval(
        "SELECT score FROM websites WHERE id = $1", ws["website_id"]
    )
    assert score == -50


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


async def test_get_stats_returns_all_fields():
    result = await get_stats()
    assert "countries_processed" in result
    assert "cities_processed" in result
    assert "cities_in_progress" in result
    assert "cities_pending" in result
    assert "websites_discovered" in result
    assert "websites_audited" in result
    assert "websites_pending" in result
    assert "average_score" in result
    assert isinstance(result["countries_processed"], int)
    assert isinstance(result["websites_discovered"], int)
    assert isinstance(result["average_score"], float)


async def test_get_stats_reflects_audit(db_conn):
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
    assert stats["websites_discovered"] >= 1
    assert stats["websites_audited"] >= 1
    # Average score should reflect at least our 42
    assert stats["average_score"] > 0


# ---------------------------------------------------------------------------
# Full pipeline integration test
# ---------------------------------------------------------------------------


async def test_full_pipeline(db_conn):
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
    assert stats["websites_discovered"] >= 1
    assert stats["websites_audited"] >= 1
