"""Tests for DiscoveryPipeline.run_for_countries() and close() lifecycle.

Covers: run_for_countries contract, max_cities_per_country honoring,
discovery_status lifecycle, multi-country runs, DB writes verification,
close delegation, and error handling.

Real database tests use a local db_conn fixture (no transaction wrapper)
because the pipeline acquires its own pool connections; data seeded via
a transactional connection would be invisible to those pool connections.
The autouse cleanup fixture and close_pool() call follow the same pattern
as tests/test_mcp_server.py.

All Places API mocks use real PlacesAPIClient instances with patched
search_text/close, not MagicMock(spec=...), to avoid property descriptor
issues with the `available` property.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.db import close_pool
from agency_audit.discovery import (
    DiscoveryPipeline,
    PlaceResult,
    PlacesAPIClient,
    run_discovery,
)

# ──────────────────────────────────────────────────────────────────────
# Local fixtures — real database, no transaction wrapper
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
async def db_conn():
    """Direct connection for test setup/teardown — no transaction wrapper.

    Uses a fresh connection (not the pool) so it works reliably across
    pytest-asyncio's per-function event loops.  No transaction is started
    so data inserted via this connection is immediately visible to the
    pipeline's own pool connections.
    """
    conn = await asyncpg.connect(dsn=settings.dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _cleanup_test_data(db_conn):
    """Reset city discovery status and remove test websites after each test.

    Also closes the shared pool so the next test gets a fresh pool on its
    own event loop.
    """
    # Reset any cities that got marked in_progress/done
    await db_conn.execute(
        "UPDATE cities SET discovery_status = 'pending' "
        "WHERE discovery_status IN ('in_progress', 'done')"
    )
    # Remove test website/city links and websites
    await db_conn.execute(
        "DELETE FROM discovery_log WHERE agent IN ('google_maps', 'google_maps_places_api')"
    )
    await db_conn.execute("DELETE FROM website_cities WHERE discovered_via = 'google_maps'")
    await db_conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
    yield
    # Clean up again after the test
    await db_conn.execute(
        "UPDATE cities SET discovery_status = 'pending' "
        "WHERE discovery_status IN ('in_progress', 'done')"
    )
    await db_conn.execute(
        "DELETE FROM discovery_log WHERE agent IN ('google_maps', 'google_maps_places_api')"
    )
    await db_conn.execute("DELETE FROM website_cities WHERE discovered_via = 'google_maps'")
    await db_conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
    # Reset the module-level pool so the next test creates a fresh one
    # on its own event loop
    await close_pool()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_place(
    place_id: str = "pid1",
    name: str = "Test Agency",
    website: str = "https://test-place.example.com",
) -> PlaceResult:
    """Create a PlaceResult with sensible defaults."""
    return PlaceResult(
        place_id=place_id,
        name=name,
        formatted_address="123 Test St",
        phone="+359 2 123 4567",
        website=website,
        latitude=42.0,
        longitude=23.0,
        rating=4.5,
        user_ratings_total=42,
    )


# ──────────────────────────────────────────────────────────────────────
# Pool plumbing tests — legitimately mock get_pool, no DB needed
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelinePoolPlumbing:
    """Tests for _get_pool() — pool creation and caching.

    These are pure plumbing tests, not SQL semantics — they verify
    that _get_pool() calls get_pool() and caches the result.
    """

    @pytest.mark.asyncio
    async def test_get_pool_creates_pool(self):
        """_get_pool calls get_pool() and caches the result."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:  # db-mock-check: ignore
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool = await pipeline._get_pool()
            assert pool is not None
            mock_get_pool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pool_cached(self):
        """_get_pool returns the same pool on subsequent calls."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:  # db-mock-check: ignore
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool1 = await pipeline._get_pool()
            pool2 = await pipeline._get_pool()
            assert pool1 is pool2
            mock_get_pool.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Close lifecycle tests — no database needed
# ──────────────────────────────────────────────────────────────────────


class TestCloseLifecycle:
    """Tests for DiscoveryPipeline.close() and PlacesAPIClient lifecycle."""

    async def test_close_delegates_to_places_client(self):
        """close() calls places.close()."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.close()

        places_client.close.assert_called_once()

    async def test_close_no_places(self):
        """close() handles None places gracefully."""
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        pipeline.places = None
        # Should not raise
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_close_lifecycle_closes_places_client(self):
        """close() calls places.close() which calls aclose on the HTTP client."""
        mock_http = AsyncMock()
        places_client = PlacesAPIClient(api_key="test")
        places_client._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.close()

        mock_http.aclose.assert_called_once()
        assert places_client._client is None

    @pytest.mark.asyncio
    async def test_close_lifecycle_idempotent(self):
        """close() can be called multiple times safely."""
        mock_http = AsyncMock()
        places_client = PlacesAPIClient(api_key="test")
        places_client._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.close()
        await pipeline.close()  # Second call: places_client._client is None, no crash

        mock_http.aclose.assert_called_once()
        assert places_client._client is None


# ──────────────────────────────────────────────────────────────────────
# run_discovery() error tests — no database needed
# ──────────────────────────────────────────────────────────────────────


class TestRunDiscoveryErrors:
    """Error paths for the run_discovery() CLI helper."""

    @pytest.mark.asyncio
    async def test_run_discovery_no_api_key_raises(self):
        """run_discovery without an API key raises RuntimeError."""
        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            places_client = PlacesAPIClient(api_key="")
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client

            with pytest.raises(RuntimeError, match="No Google Maps API key"):
                await run_discovery(countries=["BG"])


# ──────────────────────────────────────────────────────────────────────
# Real-database tests
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelineDB:
    """Tests for DiscoveryPipeline against a live PostgreSQL database."""

    # ── Basic flow ───────────────────────────────────────────────────

    async def test_single_country_single_city_two_agencies(self, db_conn):
        """Single country, one city, two agencies found — verify summary + DB writes."""
        places = [
            _make_place("pid1", "Agency One", "https://test-a1.example.com"),
            _make_place("pid2", "Agency Two", "https://test-a2.example.com"),
        ]

        async def mock_search_text(*args, **kwargs):
            return places

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = mock_search_text
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        # Contract: return structure
        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 1
        assert "BG" in result["results"]
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 2

        # Verify DB: websites were inserted
        w1 = await db_conn.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites WHERE maps_place_id = $1",
            "pid1",
        )
        assert w1 is not None
        assert w1["url"] == "https://test-a1.example.com"
        assert w1["label"] == "Agency One"

        w2 = await db_conn.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites WHERE maps_place_id = $1",
            "pid2",
        )
        assert w2 is not None
        assert w2["url"] == "https://test-a2.example.com"
        assert w2["label"] == "Agency Two"

        # Verify DB: website_cities links
        links = await db_conn.fetch(
            "SELECT website_id, city_id FROM website_cities WHERE website_id IN ($1, $2)",
            w1["id"],
            w2["id"],
        )
        assert len(links) == 2

        # Verify DB: city marked 'done'
        city_status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1",
            links[0]["city_id"],
        )
        assert city_status == "done"

        # Verify DB: discovery_log has 'found' entries
        found_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE status = 'found'"
        )
        assert found_count == 2

        # Verify DB: discovery_log has 'searched' entry
        searched_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE status = 'searched'"
        )
        assert searched_count == 1

    async def test_single_country_no_agencies(self, db_conn):
        """City processed but search returns empty — city still marked done."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 0

        # Verify city marked done
        city_id = await db_conn.fetchval(
            "SELECT id FROM cities WHERE discovery_status = 'done' LIMIT 1"
        )
        assert city_id is not None

    async def test_no_pending_cities(self, db_conn):
        """All cities already done — zero cities processed, zero agencies."""
        # Mark all cities as done
        await db_conn.execute("UPDATE cities SET discovery_status = 'done'")

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock()
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=3,
        )

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
        assert result["countries_processed"] == 0
        assert "BG" in result["results"]
        assert result["results"]["BG"]["cities"] == 0
        places_client.search_text.assert_not_called()

    # ── max_cities_per_country honoring ──────────────────────────────

    async def test_honors_max_cities(self, db_conn):
        """max_cities_per_country=2 with many pending cities → only 2 processed."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[_make_place()])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=2,
        )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2  # 1 per city
        assert result["results"]["BG"]["cities"] == 2
        assert result["results"]["BG"]["agencies"] == 2

        # Verify exactly 2 cities marked done
        done_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        assert done_count == 2

    # ── Multi-country ────────────────────────────────────────────────

    async def test_multi_country(self, db_conn):
        """Two countries (BG + RO), one city each, one agency per city."""
        # Insert a RO city since seed only has BG cities
        await db_conn.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        await db_conn.execute(
            "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
            "VALUES ('RO', 'Bucuresti', 'bucuresti', 1883425, 44.4268, 26.1025) "
            "ON CONFLICT (country, slug) DO NOTHING"
        )

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=[[bg_place], [ro_place]])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG", "RO"],
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["RO"]["cities"] == 1

        # Verify both websites inserted with correct country links
        bg_web = await db_conn.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'bg-pid'")
        assert bg_web is not None
        ro_web = await db_conn.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'ro-pid'")
        assert ro_web is not None

    async def test_one_country_no_cities_one_with(self, db_conn):
        """One country has no pending cities, the other has one."""
        # Mark all BG cities as done
        await db_conn.execute("UPDATE cities SET discovery_status = 'done' WHERE country = 'BG'")
        # Insert RO country + city
        await db_conn.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        await db_conn.execute(
            "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
            "VALUES ('RO', 'Bucuresti', 'bucuresti', 1883425, 44.4268, 26.1025) "
            "ON CONFLICT (country, slug) DO NOTHING"
        )

        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[ro_place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG", "RO"],
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        assert result["countries_processed"] == 1
        # Both countries always get an entry
        assert "BG" in result["results"]
        assert "RO" in result["results"]
        assert result["results"]["BG"]["cities"] == 0
        assert result["results"]["BG"]["agencies"] == 0
        assert result["results"]["RO"]["cities"] == 1

    # ── country_codes=None (auto-discovery) ──────────────────────────

    async def test_auto_discovers_countries_from_db(self, db_conn):
        """When country_codes=None, pending countries are fetched from DB."""
        # Insert RO with pending city so two countries have pending cities
        await db_conn.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        await db_conn.execute(
            "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
            "VALUES ('RO', 'Bucuresti', 'bucuresti', 1883425, 44.4268, 26.1025) "
            "ON CONFLICT (country, slug) DO NOTHING"
        )

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=[[bg_place], [ro_place]])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=None,
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]

    # ── Places API unavailable ────────────────────────────────────────

    async def test_places_unavailable(self, db_conn):
        """When PlacesAPIClient.available is False, city still marked done."""
        places_client = PlacesAPIClient(api_key="")  # Empty key → not available
        places_client.search_text = AsyncMock()
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        places_client.search_text.assert_not_called()

        # City should still be marked done
        done_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        assert done_count == 1

    # ── Error handling ───────────────────────────────────────────────

    async def test_search_text_error_handled(self, db_conn):
        """When search_text raises, the error is caught and city is marked done."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=RuntimeError("API error"))
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        # Should not crash
        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

        # City should still be marked done
        done_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        assert done_count == 1

    # ── DB writes — upsert & reuse ───────────────────────────────────

    async def test_upsert_new_website(self, db_conn):
        """New agency triggers INSERT INTO websites, website_cities, discovery_log."""
        place = _make_place("new-place-id", "New Agency", "https://test-new.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        # Verify website row
        web = await db_conn.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites "
            "WHERE maps_place_id = 'new-place-id'"
        )
        assert web is not None
        assert web["url"] == "https://test-new.example.com"
        assert web["label"] == "New Agency"

        # Verify website_cities link
        link = await db_conn.fetchrow(
            "SELECT city_id, discovered_via FROM website_cities WHERE website_id = $1",
            web["id"],
        )
        assert link is not None
        assert link["discovered_via"] == "google_maps"

        # Verify discovery_log 'found' entry
        found = await db_conn.fetchrow(
            "SELECT status FROM discovery_log WHERE website_id = $1 AND status = 'found'",
            web["id"],
        )
        assert found is not None

    async def test_reuse_existing_website(self, db_conn):
        """When website already exists by maps_place_id, reuse its id."""
        # Pre-insert an existing website
        existing_id = await db_conn.fetchval(
            "INSERT INTO websites (url, label, maps_place_id) "
            "VALUES ('https://test-existing.example.com', 'Existing Agency', 'existing-pid') "
            "RETURNING id"
        )

        place = _make_place(
            "existing-pid", "Existing Agency Updated", "https://test-existing.example.com"
        )
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)

        result = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        assert result["agencies_found"] == 1  # Still counted as found

        # Should NOT have created a duplicate website
        count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE maps_place_id = 'existing-pid'"
        )
        assert count == 1

        # website_cities should link to existing website
        link = await db_conn.fetchrow(
            "SELECT website_id FROM website_cities WHERE website_id = $1",
            existing_id,
        )
        assert link is not None

    async def test_upsert_on_conflict_url(self, db_conn):
        """ON CONFLICT (url) DO UPDATE — same URL updates label, NOT maps_place_id."""
        # First, insert through the pipeline
        place1 = _make_place("pid-first", "First Agency", "https://test-upsert.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place1])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result1 = await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )
        assert result1["agencies_found"] == 1

        # Second run with same URL but different place_id — should UPSERT
        places_client2 = PlacesAPIClient(api_key="test")
        places_client2.search_text = AsyncMock(
            return_value=[
                _make_place(
                    "pid-second", "Second Agency Updated", "https://test-upsert.example.com"
                )
            ]
        )
        places_client2.close = AsyncMock()

        pipeline2 = DiscoveryPipeline(places_client=places_client2)
        result2 = await pipeline2.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )
        assert result2["agencies_found"] == 1

        # Only one website row should exist
        rows = await db_conn.fetch(
            "SELECT id, label, maps_place_id FROM websites WHERE url = 'https://test-upsert.example.com'"
        )
        assert len(rows) == 1
        # ON CONFLICT (url) DO UPDATE SET label = EXCLUDED.label — maps_place_id stays
        assert rows[0]["label"] == "Second Agency Updated"  # Updated label
        assert rows[0]["maps_place_id"] == "pid-first"  # NOT updated by ON CONFLICT

    # ── Status lifecycle ─────────────────────────────────────────────

    async def test_discovery_status_lifecycle(self, db_conn):
        """City goes from 'pending' to 'done' after pipeline processes it."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.run_for_countries(
            country_codes=["BG"],
            max_cities_per_country=1,
        )

        # At least one city (the one the pipeline processed) should now be 'done'
        done_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        assert done_count == 1, f"Expected exactly 1 done city, got {done_count}"

    # ── run_discovery() CLI helper (real DB path) ────────────────────

    async def test_run_discovery_cli_helper(self, db_conn):
        """run_discovery() with API key and real DB works end-to-end."""
        place = _make_place("cli-pid", "CLI Agency", "https://test-cli.example.com")

        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            places_client = PlacesAPIClient(api_key="test")
            places_client.search_text = AsyncMock(return_value=[place])
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client

            result = await run_discovery(countries=["BG"], max_cities=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        places_client.close.assert_called()

        # Verify data in DB
        web = await db_conn.fetchrow("SELECT id, url FROM websites WHERE maps_place_id = 'cli-pid'")
        assert web is not None
        assert web["url"] == "https://test-cli.example.com"

    async def test_run_discovery_cli_helper_no_cities(self):
        """run_discovery() with API key but no pending cities."""
        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            places_client = PlacesAPIClient(api_key="test")
            places_client.search_text = AsyncMock()
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client

            result = await run_discovery(countries=["ZZ"])  # Non-existent country

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
