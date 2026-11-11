"""Tests for DiscoveryPipeline.run_for_countries() and close() lifecycle.

Covers: run_for_countries contract, max_cities_per_country honoring,
discovery_status lifecycle, multi-country runs, DB writes verification,
close delegation, and error handling.

Real-database tests use the ``fresh_db`` fixture (conftest.py), which
provides a private, pristine database per test, points ``get_pool()`` at
it, seeds canonical countries + 20 BG cities, and drops it on teardown.
Tests seed their own cities directly on ``fresh_db``'s connection so the
pipeline's pool connections can see them; no manual cleanup is needed
and exact-count assertions are safe.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx
import pytest

from agency_audit.discovery import (
    DiscoveryPipeline,
    PlaceResult,
    PlacesAPIClient,
    run_discovery,
)
from agency_audit.discovery_geo import Rectangle

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_viewport() -> Rectangle:
    """Small Sofia-area viewport for mocking resolve_city_viewport."""
    return Rectangle(
        low_lat=42.6,
        low_lng=23.2,
        high_lat=42.8,
        high_lng=23.5,
    )


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


async def _seed_test_city(
    conn: asyncpg.Connection,
    slug: str,
    country: str = "BG",
    population: int = 9_999_999,
) -> int:
    """Insert a test city that the pipeline will pick first (high population).

    Writes directly on ``fresh_db``'s connection (auto-commit mode), so
    the pipeline's pool connections can see the committed row immediately.

    Uses ON CONFLICT DO NOTHING so a previously inserted city is never
    re-activated.

    Returns the city's id.
    """
    await conn.execute(
        "INSERT INTO cities (country, label, slug, population) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (country, slug) DO NOTHING",
        country,
        slug.replace("-", " ").title(),
        slug,
        population,
    )
    result = await conn.fetchval(
        "SELECT id FROM cities WHERE country = $1 AND slug = $2", country, slug
    )
    assert result is not None
    return result


# ──────────────────────────────────────────────────────────────────────
# Pool plumbing tests — legitimately mock get_pool, no DB needed
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelinePoolPlumbing:
    """Tests for _get_pool() — pool creation and caching.

    These are pure plumbing tests, not SQL semantics — they verify
    that _get_pool() calls get_pool() and caches the result.
    """

    @pytest.mark.asyncio
    async def test_get_pool_creates_pool(self) -> None:
        """_get_pool calls get_pool() and caches the result."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:  # db-mock-check: ignore
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool = await pipeline._get_pool()
            assert pool is not None
            mock_get_pool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pool_cached(self) -> None:
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

    async def test_close_delegates_to_places_client(self) -> None:
        """close() calls places.close()."""
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        await pipeline.close()

        places_client.close.assert_called_once()

    async def test_close_no_places(self) -> None:
        """close() handles None places gracefully."""
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        pipeline.places = None
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_close_lifecycle_closes_places_client(self) -> None:
        """close() calls places.close() which calls aclose on the HTTP client."""
        mock_http = AsyncMock()
        places_client = PlacesAPIClient(api_key="test")
        places_client._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        await pipeline.close()

        mock_http.aclose.assert_called_once()
        assert places_client._client is None

    @pytest.mark.asyncio
    async def test_close_lifecycle_idempotent(self) -> None:
        """close() can be called multiple times safely."""
        mock_http = AsyncMock()
        places_client = PlacesAPIClient(api_key="test")
        places_client._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        await pipeline.close()
        await pipeline.close()

        mock_http.aclose.assert_called_once()
        assert places_client._client is None


# ──────────────────────────────────────────────────────────────────────
# run_discovery() error tests — no database needed
# ──────────────────────────────────────────────────────────────────────


class TestRunDiscoveryErrors:
    """Error paths for the run_discovery() CLI helper."""

    @pytest.mark.asyncio
    async def test_run_discovery_no_api_key_raises(self) -> None:
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
    """Tests for DiscoveryPipeline against a live PostgreSQL database.

    Each test receives a private, pristine database from the ``fresh_db``
    fixture: settings are already monkeypatched, the canonical seed (44
    countries + 20 BG cities) is applied, and the database is dropped on
    teardown.  Tests seed their own cities directly on ``fresh_db``'s
    connection (auto-commit mode) so the pipeline's pool connections can
    see the committed rows — no manual cleanup is needed.
    """

    # ── Basic flow ───────────────────────────────────────────────────

    async def test_single_country_single_city_two_agencies(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """Single country, one city, two agencies found — verify summary + DB writes."""
        city_id = await _seed_test_city(fresh_db, "test-two-agencies")

        places = [
            _make_place("pid1", "Agency One", "https://test-a1.example.com"),
            _make_place("pid2", "Agency Two", "https://test-a2.example.com"),
        ]

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=places)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 1
        assert "BG" in result["results"]
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 2

        w1 = await fresh_db.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites WHERE maps_place_id = $1",
            "pid1",
        )
        assert w1 is not None
        assert w1["url"] == "https://test-a1.example.com"
        assert w1["label"] == "Agency One"

        w2 = await fresh_db.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites WHERE maps_place_id = $1",
            "pid2",
        )
        assert w2 is not None
        assert w2["url"] == "https://test-a2.example.com"
        assert w2["label"] == "Agency Two"

        links = await fresh_db.fetch(
            "SELECT website_id, city_id FROM website_cities "
            "WHERE city_id = $1 AND website_id IN ($2, $3)",
            city_id,
            w1["id"],
            w2["id"],
        )
        assert len(links) == 2

        city_status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert city_status == "done"

        found_count = await fresh_db.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE website_id IN ($1, $2) AND status = 'found'",
            w1["id"],
            w2["id"],
        )
        assert found_count == 2

        searched_count = await fresh_db.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE city_id = $1 AND status = 'searched'",
            city_id,
        )
        assert searched_count == 1

    async def test_single_country_no_agencies(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """City processed but search returns empty — city still marked done."""
        city_id = await _seed_test_city(fresh_db, "test-no-agencies")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[])
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 0

        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    async def test_no_pending_cities(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """All cities already done — zero cities processed, zero agencies.

        Uses a synthetic country (ZZ) with a single city pre-marked 'done'
        so the test only mutates its own rows, not the seed data.
        """
        test_country = "ZZ"
        await fresh_db.execute(
            "INSERT INTO countries (iso, label) VALUES ($1, $2) ON CONFLICT (iso) DO NOTHING",
            test_country,
            "Testland",
        )
        await fresh_db.execute(
            "INSERT INTO cities "
            "(country, label, slug, population, discovery_status) "
            "VALUES ($1, 'Done City', 'done-city-zz', 1000, 'done') "
            "ON CONFLICT (country, slug) DO NOTHING",
            test_country,
        )

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        result = await pipeline.run_for_countries(
            country_codes=[test_country], max_cities_per_country=3
        )

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
        assert result["countries_processed"] == 0
        assert test_country in result["results"]
        assert result["results"][test_country]["cities"] == 0
        # No pending cities → search_tiled never called
        # (we don't assert_not_called on search_tiled since it's not mocked)

    # ── max_cities_per_country honoring ──────────────────────────────

    async def test_honors_max_cities(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """max_cities_per_country=2 with many pending cities → only 2 processed."""
        c1 = await _seed_test_city(fresh_db, "test-max-cities-1", population=9_999_999)
        c2 = await _seed_test_city(fresh_db, "test-max-cities-2", population=9_999_998)
        c3 = await _seed_test_city(fresh_db, "test-max-cities-3", population=9_999_997)

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[_make_place()])
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=2)

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert result["results"]["BG"]["cities"] == 2
        assert result["results"]["BG"]["agencies"] == 2

        for cid in (c1, c2):
            status = await fresh_db.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

        status3 = await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE id = $1", c3)
        assert status3 == "pending"

    # ── Multi-country ────────────────────────────────────────────────

    async def test_multi_country(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """Two countries (BG + RO), one city each, one agency per city."""
        await fresh_db.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        bg_id = await _seed_test_city(fresh_db, "test-multi-bg", country="BG")
        ro_id = await _seed_test_city(fresh_db, "test-multi-ro", country="RO")

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(side_effect=[[bg_place], [ro_place]])
        result = await pipeline.run_for_countries(
            country_codes=["BG", "RO"], max_cities_per_country=1
        )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["RO"]["cities"] == 1

        bg_web = await fresh_db.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'bg-pid'")
        assert bg_web is not None
        ro_web = await fresh_db.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'ro-pid'")
        assert ro_web is not None

        for cid in (bg_id, ro_id):
            status = await fresh_db.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

    async def test_one_country_no_cities_one_with(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """One country has no pending cities, the other has one."""
        no_city_iso = "YY"
        # Country with a city already done — no pending cities
        await fresh_db.execute(
            "INSERT INTO countries (iso, label) VALUES ($1, $2) ON CONFLICT (iso) DO NOTHING",
            no_city_iso,
            "Nocitiesland",
        )
        await fresh_db.execute(
            "INSERT INTO cities "
            "(country, label, slug, population, discovery_status) "
            "VALUES ($1, 'Done City', 'done-city-yy', 1000, 'done') "
            "ON CONFLICT (country, slug) DO NOTHING",
            no_city_iso,
        )

        # Country with a pending city
        await fresh_db.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        ro_id = await _seed_test_city(fresh_db, "test-one-pending", country="RO")

        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[ro_place])
        result = await pipeline.run_for_countries(
            country_codes=[no_city_iso, "RO"], max_cities_per_country=1
        )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        assert result["countries_processed"] == 1
        assert no_city_iso in result["results"]
        assert "RO" in result["results"]
        assert result["results"][no_city_iso]["cities"] == 0
        assert result["results"][no_city_iso]["agencies"] == 0
        assert result["results"]["RO"]["cities"] == 1

        status = await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE id = $1", ro_id)
        assert status == "done"

    # ── country_codes=None (auto-discovery) ──────────────────────────

    async def test_auto_discovers_countries_from_db(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When country_codes=None, pending countries are fetched from DB."""
        await fresh_db.execute(
            "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
            "ON CONFLICT (iso) DO NOTHING"
        )
        bg_id = await _seed_test_city(fresh_db, "test-auto-bg", country="BG")
        ro_id = await _seed_test_city(fresh_db, "test-auto-ro", country="RO")

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(side_effect=[[bg_place], [ro_place]])
        result = await pipeline.run_for_countries(country_codes=None, max_cities_per_country=1)

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]

        for cid in (bg_id, ro_id):
            status = await fresh_db.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

    # ── Places API unavailable ────────────────────────────────────────

    async def test_places_unavailable(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When PlacesAPIClient.available is False, city still marked done."""
        city_id = await _seed_test_city(fresh_db, "test-places-unavail")

        places_client = PlacesAPIClient(api_key="")  # Empty key → not available
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    # ── Error handling ───────────────────────────────────────────────

    async def test_search_text_error_handled(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When search_tiled raises, the error is caught and city is marked done."""
        city_id = await _seed_test_city(fresh_db, "test-search-error")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(side_effect=RuntimeError("API error"))
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    async def test_viewport_resolution_failure_marks_city_failed(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When resolve_city_viewport raises, city is marked 'failed' not stuck in_progress."""
        city_id = await _seed_test_city(fresh_db, "test-viewport-fail")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(
            side_effect=RuntimeError("Geocoding failed for 'Test Viewport Fail, BG'")
        )
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        # City must be marked failed, not stuck in_progress
        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "failed"

        # Failed cities count toward the per-country cap — one attempt consumed
        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

    async def test_failed_city_counts_toward_cap_and_next_remains_pending(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When max_cities=1 and the first pending city fails, next city stays pending.

        A geocoding outage must NOT consume every remaining pending city in
        the country queue — the failed attempt must count toward the cap so
        the run stops after max_cities_per_country attempts.
        """
        city_1 = await _seed_test_city(fresh_db, "test-fail-cap-a", population=10_000_000)
        city_2 = await _seed_test_city(fresh_db, "test-fail-cap-b", population=9_000_000)

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(side_effect=RuntimeError("Geocoding failed"))
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        # City 1 (highest population) was attempted and marked failed
        assert (
            await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE id = $1", city_1)
            == "failed"
        )

        # City 2 must still be pending — NOT consumed by the failure loop
        assert (
            await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE id = $1", city_2)
            == "pending"
        )

        # Exactly one attempt was counted
        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

    # ── DB writes — upsert & reuse ───────────────────────────────────

    async def test_upsert_new_website(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """New agency triggers INSERT INTO websites, website_cities, discovery_log."""
        city_id = await _seed_test_city(fresh_db, "test-upsert-new")

        place = _make_place("new-place-id", "New Agency", "https://test-new.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[place])
        await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        web = await fresh_db.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites "
            "WHERE maps_place_id = 'new-place-id'"
        )
        assert web is not None
        assert web["url"] == "https://test-new.example.com"
        assert web["label"] == "New Agency"

        link = await fresh_db.fetchrow(
            "SELECT city_id, discovered_via FROM website_cities WHERE website_id = $1",
            web["id"],
        )
        assert link is not None
        assert link["discovered_via"] == "google_maps"
        assert link["city_id"] == city_id

        found = await fresh_db.fetchrow(
            "SELECT status FROM discovery_log "
            "WHERE website_id = $1 AND city_id = $2 AND status = 'found'",
            web["id"],
            city_id,
        )
        assert found is not None

    async def test_reuse_existing_website(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """When website already exists by maps_place_id, reuse its id."""
        _city_id = await _seed_test_city(fresh_db, "test-reuse-existing")
        existing_id = await fresh_db.fetchval(
            "INSERT INTO websites (url, url_key, label, maps_place_id) "
            "VALUES ('https://test-existing.example.com', 'test-existing.example.com', "
            "'Existing Agency', "
            "'existing-pid') RETURNING id"
        )

        place = _make_place(
            "existing-pid",
            "Existing Agency Updated",
            "https://test-existing.example.com",
        )
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[place])
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["agencies_found"] == 1

        count = await fresh_db.fetchval(
            "SELECT COUNT(*) FROM websites WHERE maps_place_id = 'existing-pid'"
        )
        assert count == 1

        link = await fresh_db.fetchrow(
            "SELECT website_id FROM website_cities WHERE website_id = $1", existing_id
        )
        assert link is not None

    async def test_upsert_on_conflict_url(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """ON CONFLICT (url_key) DO UPDATE — same URL updates label, NOT maps_place_id."""
        _city1 = await _seed_test_city(fresh_db, "test-upsert-url-1")
        _city2 = await _seed_test_city(fresh_db, "test-upsert-url-2")

        # First run: insert with pid-first
        place1 = _make_place("pid-first", "First Agency", "https://test-upsert.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[place1])
        result1 = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)
        assert result1["agencies_found"] == 1

        # Second run: same URL, different place_id → UPSERT
        places_client2 = PlacesAPIClient(api_key="test")
        places_client2.close = AsyncMock()

        pipeline2 = DiscoveryPipeline(places_client=places_client2)
        pipeline2.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline2.search_tiled = AsyncMock(
            return_value=[
                _make_place(
                    "pid-second",
                    "Second Agency Updated",
                    "https://test-upsert.example.com",
                )
            ]
        )
        result2 = await pipeline2.run_for_countries(country_codes=["BG"], max_cities_per_country=1)
        assert result2["agencies_found"] == 1

        rows = await fresh_db.fetch(
            "SELECT id, label, maps_place_id FROM websites "
            "WHERE url = 'https://test-upsert.example.com'"
        )
        assert len(rows) == 1
        assert rows[0]["label"] == "Second Agency Updated"
        assert rows[0]["maps_place_id"] == "pid-first"

    async def test_branches_with_tracking_links_share_one_website(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """Two branches whose links differ only by utm_* parameters → one website row."""
        antwerpen = await _seed_test_city(fresh_db, "test-branch-1", population=9_999_999)
        hasselt = await _seed_test_city(fresh_db, "test-branch-2", population=9_999_998)
        link = "https://www.test-heylen.example.com/?utm_source=organic&utm_campaign=gmb+{}"

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()
        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())

        async def branch_of(query: str, city: dict, max_calls: int | None = None) -> list:
            branch = "antwerpen" if city["id"] == antwerpen else "hasselt"
            return [_make_place(f"pid-{branch}", "Heylen", link.format(branch))]

        pipeline.search_tiled = AsyncMock(side_effect=branch_of)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=2)
        assert result["agencies_found"] == 2

        rows = await fresh_db.fetch(
            "SELECT id, url FROM websites WHERE url_key = 'test-heylen.example.com'"
        )
        assert len(rows) == 1
        assert rows[0]["url"] == "https://www.test-heylen.example.com/"
        cities = await fresh_db.fetch(
            "SELECT city_id FROM website_cities WHERE website_id = $1 ORDER BY city_id",
            rows[0]["id"],
        )
        assert [r["city_id"] for r in cities] == sorted([antwerpen, hasselt])

    # ── Status lifecycle ─────────────────────────────────────────────

    async def test_discovery_status_lifecycle(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """City goes from 'pending' to 'done' after pipeline processes it."""
        city_id = await _seed_test_city(fresh_db, "test-status-lifecycle")

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(return_value=[])
        await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    # ── run_discovery() CLI helper (real DB path) ────────────────────

    async def test_run_discovery_cli_helper(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """run_discovery() with API key and real DB works end-to-end."""
        _city_id = await _seed_test_city(fresh_db, "test-cli-helper")

        place = _make_place("cli-pid", "CLI Agency", "https://test-cli.example.com")

        with (
            patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls,
            patch.object(DiscoveryPipeline, "resolve_city_viewport") as mock_resolve,
            patch.object(DiscoveryPipeline, "search_tiled") as mock_search_tiled,
        ):
            places_client = PlacesAPIClient(api_key="test")
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client
            mock_search_tiled.return_value = [place]
            mock_resolve.return_value = _make_viewport()

            result = await run_discovery(countries=["BG"], max_cities=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        places_client.close.assert_called()

        web = await fresh_db.fetchrow(
            "SELECT id, url FROM websites WHERE maps_place_id = 'cli-pid'"
        )
        assert web is not None
        assert web["url"] == "https://test-cli.example.com"

    async def test_run_discovery_cli_helper_no_cities(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """run_discovery() with API key but no pending cities."""
        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            places_client = PlacesAPIClient(api_key="test")
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client

            result = await run_discovery(countries=["ZZ"])  # Non-existent country

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0

    # ── Keyword loop: every query runs, no early stop ─────────────────────

    async def test_all_keywords_searched_no_early_stop(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """Every COUNTRY_QUERIES keyword is searched; no early break at 20 results.

        BG has two keywords.  When the first keyword returns 20 places the
        loop MUST continue to the second keyword instead of breaking early.
        """
        _city_id = await _seed_test_city(fresh_db, "test-all-keywords")

        # 20 distinct places for keyword 1, 20 for keyword 2
        kw1 = [
            _make_place(f"kw1-{i}", f"KW1 Agency {i}", f"https://kw1-{i}.example.com")
            for i in range(20)
        ]
        kw2 = [
            _make_place(f"kw2-{i}", f"KW2 Agency {i}", f"https://kw2-{i}.example.com")
            for i in range(20)
        ]

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(side_effect=[kw1, kw2])
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        # Both keywords MUST have been searched
        assert pipeline.search_tiled.call_count == 2, (
            f"Expected 2 search_tiled calls (one per keyword), got "
            f"{pipeline.search_tiled.call_count}"
        )

        # Results from BOTH keywords must appear — 40 total, 20 per keyword
        assert result["agencies_found"] == 40

        rows = await fresh_db.fetch("SELECT maps_place_id FROM websites")
        place_ids = {row["maps_place_id"] for row in rows}

        assert any(pid.startswith("kw1-") for pid in place_ids), (
            "Keyword 1 places not found in database"
        )
        assert any(pid.startswith("kw2-") for pid in place_ids), (
            "Keyword 2 places not found in database"
        )

    async def test_dedup_across_keywords(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """Same place_id from different keywords is only inserted once."""
        _city_id = await _seed_test_city(fresh_db, "test-dedup-keywords")

        shared = _make_place("shared-pid", "Shared Agency", "https://shared.example.com")
        unique_kw1 = _make_place("unique-1", "Unique One", "https://unique-1.example.com")
        unique_kw2 = _make_place("unique-2", "Unique Two", "https://unique-2.example.com")

        # Keyword 1: shared + unique-1; Keyword 2: shared + unique-2
        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(
            side_effect=[
                [shared, unique_kw1],
                [shared, unique_kw2],
            ]
        )
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        # Both keywords searched
        assert pipeline.search_tiled.call_count == 2

        # 3 unique places, not 4 — the shared one deduped
        assert result["agencies_found"] == 3

        count = await fresh_db.fetchval(
            "SELECT COUNT(*) FROM websites WHERE maps_place_id = 'shared-pid'"
        )
        assert count == 1, "Shared place_id should be inserted exactly once"

    async def test_places_auth_failure_marks_city_failed_with_reason(
        self,
        fresh_db: asyncpg.Connection,
    ) -> None:
        """A Places API auth/permission error (401/403) propagates to
        city 'failed' with the reason, not silently swallowed into 'done'."""
        city_id = await _seed_test_city(fresh_db, "test-places-auth-fail")

        # Build an httpx.HTTPStatusError mimicking a 403 PERMISSION_DENIED
        # from the Places API.
        request = httpx.Request("POST", "https://places.googleapis.com/v1/places:searchText")
        response = httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "Places API is not enabled for this API key.",
                    "status": "PERMISSION_DENIED",
                }
            },
            request=request,
        )
        auth_error = httpx.HTTPStatusError(
            "403 Forbidden",
            request=request,
            response=response,
        )

        places_client = PlacesAPIClient(api_key="test")
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        pipeline.resolve_city_viewport = AsyncMock(return_value=_make_viewport())
        pipeline.search_tiled = AsyncMock(side_effect=auth_error)

        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        # City must be marked failed, not done
        status = await fresh_db.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "failed", f"Expected 'failed', got '{status}'"

        # The error reason must be persisted
        error_reason = await fresh_db.fetchval(
            "SELECT discovery_error FROM cities WHERE id = $1", city_id
        )
        assert error_reason is not None, "discovery_error must be set"
        assert "403" in error_reason
        assert "Forbidden" in error_reason

        # The failed attempt counts toward the cap
        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
