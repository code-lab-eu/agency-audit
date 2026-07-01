"""Tests for DiscoveryPipeline.run_for_countries() and close() lifecycle.

Covers: run_for_countries contract, max_cities_per_country honoring,
discovery_status lifecycle, multi-country runs, DB writes verification,
close delegation, and error handling.

Real-database tests follow the pattern from test_cli_commands.py:
accept the shared db_conn (sentinel), postgres_dsn (fixture DB), and
monkeypatch (to point get_pool() at the same DB).  Each test seeds its
own cities on a separate auto-committing postgres_dsn connection so the
pipeline's pool connections can see them.  A per-test cleanup fixture
removes test-owned rows after each test so the developer's database
stays clean on the non-Docker fallback path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

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


def _point_settings_at_fixture_db(monkeypatch: pytest.MonkeyPatch, postgres_dsn: str) -> None:
    """Monkeypatch agency_audit.config.settings so get_pool() connects to the fixture DB."""
    parsed = urlparse(postgres_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
    monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")


async def _seed_test_city(
    conn: asyncpg.Connection,
    slug: str,
    country: str = "BG",
    population: int = 9_999_999,
) -> int:
    """Insert a test city that the pipeline will pick first (high population).

    Uses ON CONFLICT DO NOTHING so a previously-processed city
    (from a prior test or seed data) is never re-activated.

    Returns the city's id.
    """
    await conn.execute(
        "INSERT INTO cities (country, label, slug, population, latitude, longitude) "
        "VALUES ($1, $2, $3, $4, 42.0, 23.0) "
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
# Per-test fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _close_pool_after_test():
    """Close the global pool after each test so the next test starts fresh.

    The pipeline's _get_pool() caches the pool at module level in db.py;
    pytest-asyncio creates a new event loop per test, so a stale pool
    from a previous loop would fail on the next test.
    """
    yield
    await close_pool()


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

    Each test seeds its own cities on a separate auto-committing
    connection (postgres_dsn) so the pipeline's pool connections can
    see them, and monkeypatches settings so get_pool() connects to the
    fixture database.  A per-test cleanup fixture removes test-owned
    rows after each test so the developer's database stays clean on
    the non-Docker fallback path.
    """

    @pytest.fixture(autouse=True)
    async def _cleanup_test_data(self, postgres_dsn: str):  # type: ignore[return-type]
        """Remove test-owned rows that outlive db_conn's rollback.

        Tests seed data via auto-committing connections so the pipeline's
        pool connections can see committed rows.  This fixture deletes
        them on teardown: cities by slug pattern (CASCADE removes
        website_cities, SET NULL on discovery_log), then orphaned
        websites, then test-owned countries.
        """
        yield
        cleanup_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            # Delete discovery_log rows first — the FKs are ON DELETE SET NULL,
            # so deleting cities/websites first would orphan the log rows.
            await cleanup_conn.execute(
                "DELETE FROM discovery_log "
                "WHERE city_id IN ("
                "    SELECT id FROM cities WHERE slug LIKE 'test-%' OR slug LIKE 'done-city-%'"
                ")"
                "   OR website_id IN ("
                "       SELECT id FROM websites WHERE url LIKE 'https://test-%'"
                "   )"
                "   OR city_id IS NULL AND website_id IS NULL"
            )
            # Now safe to delete parent rows.
            await cleanup_conn.execute(
                "DELETE FROM cities WHERE slug LIKE 'test-%' OR slug LIKE 'done-city-%'"
            )
            await cleanup_conn.execute(
                "DELETE FROM websites "
                "WHERE id NOT IN (SELECT DISTINCT website_id FROM website_cities) "
                "AND url LIKE 'https://test-%'"
            )
            await cleanup_conn.execute("DELETE FROM countries WHERE iso IN ('ZZ', 'YY')")
        finally:
            await cleanup_conn.close()

    # ── Basic flow ───────────────────────────────────────────────────

    async def test_single_country_single_city_two_agencies(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single country, one city, two agencies found — verify summary + DB writes."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-two-agencies")
        finally:
            await seed_conn.close()

        places = [
            _make_place("pid1", "Agency One", "https://test-a1.example.com"),
            _make_place("pid2", "Agency Two", "https://test-a2.example.com"),
        ]

        async def mock_search_text(*args: Any, **kwargs: Any) -> list[PlaceResult]:
            return places

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = mock_search_text
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 1
        assert "BG" in result["results"]
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 2

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

        links = await db_conn.fetch(
            "SELECT website_id, city_id FROM website_cities "
            "WHERE city_id = $1 AND website_id IN ($2, $3)",
            city_id,
            w1["id"],
            w2["id"],
        )
        assert len(links) == 2

        city_status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert city_status == "done"

        found_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE website_id IN ($1, $2) AND status = 'found'",
            w1["id"],
            w2["id"],
        )
        assert found_count == 2

        searched_count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM discovery_log WHERE city_id = $1 AND status = 'searched'",
            city_id,
        )
        assert searched_count == 1

    async def test_single_country_no_agencies(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """City processed but search returns empty — city still marked done."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-no-agencies")
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        assert result["results"]["BG"]["cities"] == 1
        assert result["results"]["BG"]["agencies"] == 0

        status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    async def test_no_pending_cities(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All cities already done — zero cities processed, zero agencies.

        Uses a synthetic country (ZZ) with a single city pre-marked 'done'
        so the test only mutates its own rows, not the seed data.
        """
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        test_country = "ZZ"
        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await seed_conn.execute(
                "INSERT INTO countries (iso, label) VALUES ($1, $2) ON CONFLICT (iso) DO NOTHING",
                test_country,
                "Testland",
            )
            await seed_conn.execute(
                "INSERT INTO cities "
                "(country, label, slug, population, latitude, longitude, discovery_status) "
                "VALUES ($1, 'Done City', 'done-city-zz', 1000, 42.0, 23.0, 'done') "
                "ON CONFLICT (country, slug) DO NOTHING",
                test_country,
            )
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock()
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(
            country_codes=[test_country], max_cities_per_country=3
        )

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
        assert result["countries_processed"] == 0
        assert test_country in result["results"]
        assert result["results"][test_country]["cities"] == 0
        places_client.search_text.assert_not_called()

    # ── max_cities_per_country honoring ──────────────────────────────

    async def test_honors_max_cities(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """max_cities_per_country=2 with many pending cities → only 2 processed."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            c1 = await _seed_test_city(seed_conn, "test-max-cities-1", population=9_999_999)
            c2 = await _seed_test_city(seed_conn, "test-max-cities-2", population=9_999_998)
            c3 = await _seed_test_city(seed_conn, "test-max-cities-3", population=9_999_997)
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[_make_place()])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=2)

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert result["results"]["BG"]["cities"] == 2
        assert result["results"]["BG"]["agencies"] == 2

        for cid in (c1, c2):
            status = await db_conn.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

        status3 = await db_conn.fetchval("SELECT discovery_status FROM cities WHERE id = $1", c3)
        assert status3 == "pending"

    # ── Multi-country ────────────────────────────────────────────────

    async def test_multi_country(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two countries (BG + RO), one city each, one agency per city."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await seed_conn.execute(
                "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
                "ON CONFLICT (iso) DO NOTHING"
            )
            bg_id = await _seed_test_city(seed_conn, "test-multi-bg", country="BG")
            ro_id = await _seed_test_city(seed_conn, "test-multi-ro", country="RO")
        finally:
            await seed_conn.close()

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=[[bg_place], [ro_place]])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
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

        bg_web = await db_conn.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'bg-pid'")
        assert bg_web is not None
        ro_web = await db_conn.fetchrow("SELECT id FROM websites WHERE maps_place_id = 'ro-pid'")
        assert ro_web is not None

        for cid in (bg_id, ro_id):
            status = await db_conn.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

    async def test_one_country_no_cities_one_with(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One country has no pending cities, the other has one."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        no_city_iso = "YY"
        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            # Country with a city already done — no pending cities
            await seed_conn.execute(
                "INSERT INTO countries (iso, label) VALUES ($1, $2) ON CONFLICT (iso) DO NOTHING",
                no_city_iso,
                "Nocitiesland",
            )
            await seed_conn.execute(
                "INSERT INTO cities "
                "(country, label, slug, population, latitude, longitude, discovery_status) "
                "VALUES ($1, 'Done City', 'done-city-yy', 1000, 42.0, 23.0, 'done') "
                "ON CONFLICT (country, slug) DO NOTHING",
                no_city_iso,
            )

            # Country with a pending city
            await seed_conn.execute(
                "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
                "ON CONFLICT (iso) DO NOTHING"
            )
            ro_id = await _seed_test_city(seed_conn, "test-one-pending", country="RO")
        finally:
            await seed_conn.close()

        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[ro_place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
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

        status = await db_conn.fetchval("SELECT discovery_status FROM cities WHERE id = $1", ro_id)
        assert status == "done"

    # ── country_codes=None (auto-discovery) ──────────────────────────

    async def test_auto_discovers_countries_from_db(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When country_codes=None, pending countries are fetched from DB."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await seed_conn.execute(
                "INSERT INTO countries (iso, label) VALUES ('RO', 'Romania') "
                "ON CONFLICT (iso) DO NOTHING"
            )
            bg_id = await _seed_test_city(seed_conn, "test-auto-bg", country="BG")
            ro_id = await _seed_test_city(seed_conn, "test-auto-ro", country="RO")
        finally:
            await seed_conn.close()

        bg_place = _make_place("bg-pid", "BG Agency", "https://test-bg.example.com")
        ro_place = _make_place("ro-pid", "RO Agency", "https://test-ro.example.com")

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=[[bg_place], [ro_place]])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=None, max_cities_per_country=1)

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]

        for cid in (bg_id, ro_id):
            status = await db_conn.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", cid
            )
            assert status == "done"

    # ── Places API unavailable ────────────────────────────────────────

    async def test_places_unavailable(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When PlacesAPIClient.available is False, city still marked done."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-places-unavail")
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="")  # Empty key → not available
        places_client.search_text = AsyncMock()
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        places_client.search_text.assert_not_called()

        status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    # ── Error handling ───────────────────────────────────────────────

    async def test_search_text_error_handled(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When search_text raises, the error is caught and city is marked done."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-search-error")
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(side_effect=RuntimeError("API error"))
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0

        status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    # ── DB writes — upsert & reuse ───────────────────────────────────

    async def test_upsert_new_website(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """New agency triggers INSERT INTO websites, website_cities, discovery_log."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-upsert-new")
        finally:
            await seed_conn.close()

        place = _make_place("new-place-id", "New Agency", "https://test-new.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        web = await db_conn.fetchrow(
            "SELECT id, url, label, maps_place_id FROM websites "
            "WHERE maps_place_id = 'new-place-id'"
        )
        assert web is not None
        assert web["url"] == "https://test-new.example.com"
        assert web["label"] == "New Agency"

        link = await db_conn.fetchrow(
            "SELECT city_id, discovered_via FROM website_cities WHERE website_id = $1",
            web["id"],
        )
        assert link is not None
        assert link["discovered_via"] == "google_maps"
        assert link["city_id"] == city_id

        found = await db_conn.fetchrow(
            "SELECT status FROM discovery_log "
            "WHERE website_id = $1 AND city_id = $2 AND status = 'found'",
            web["id"],
            city_id,
        )
        assert found is not None

    async def test_reuse_existing_website(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When website already exists by maps_place_id, reuse its id."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            _city_id = await _seed_test_city(seed_conn, "test-reuse-existing")
            existing_id = await seed_conn.fetchval(
                "INSERT INTO websites (url, label, maps_place_id) "
                "VALUES ('https://test-existing.example.com', 'Existing Agency', "
                "'existing-pid') RETURNING id"
            )
        finally:
            await seed_conn.close()

        place = _make_place(
            "existing-pid",
            "Existing Agency Updated",
            "https://test-existing.example.com",
        )
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        assert result["agencies_found"] == 1

        count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE maps_place_id = 'existing-pid'"
        )
        assert count == 1

        link = await db_conn.fetchrow(
            "SELECT website_id FROM website_cities WHERE website_id = $1", existing_id
        )
        assert link is not None

    async def test_upsert_on_conflict_url(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ON CONFLICT (url) DO UPDATE — same URL updates label, NOT maps_place_id."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            _city1 = await _seed_test_city(seed_conn, "test-upsert-url-1")
            _city2 = await _seed_test_city(seed_conn, "test-upsert-url-2")
        finally:
            await seed_conn.close()

        # First run: insert with pid-first
        place1 = _make_place("pid-first", "First Agency", "https://test-upsert.example.com")
        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[place1])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        result1 = await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)
        assert result1["agencies_found"] == 1

        # Second run: same URL, different place_id → UPSERT
        places_client2 = PlacesAPIClient(api_key="test")
        places_client2.search_text = AsyncMock(
            return_value=[
                _make_place(
                    "pid-second",
                    "Second Agency Updated",
                    "https://test-upsert.example.com",
                )
            ]
        )
        places_client2.close = AsyncMock()

        pipeline2 = DiscoveryPipeline(places_client=places_client2)
        result2 = await pipeline2.run_for_countries(country_codes=["BG"], max_cities_per_country=1)
        assert result2["agencies_found"] == 1

        rows = await db_conn.fetch(
            "SELECT id, label, maps_place_id FROM websites "
            "WHERE url = 'https://test-upsert.example.com'"
        )
        assert len(rows) == 1
        assert rows[0]["label"] == "Second Agency Updated"
        assert rows[0]["maps_place_id"] == "pid-first"

    # ── Status lifecycle ─────────────────────────────────────────────

    async def test_discovery_status_lifecycle(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """City goes from 'pending' to 'done' after pipeline processes it."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            city_id = await _seed_test_city(seed_conn, "test-status-lifecycle")
        finally:
            await seed_conn.close()

        places_client = PlacesAPIClient(api_key="test")
        places_client.search_text = AsyncMock(return_value=[])
        places_client.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=places_client)
        await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

        status = await db_conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        assert status == "done"

    # ── run_discovery() CLI helper (real DB path) ────────────────────

    async def test_run_discovery_cli_helper(
        self,
        db_conn: asyncpg.Connection,
        postgres_dsn: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """run_discovery() with API key and real DB works end-to-end."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        seed_conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            _city_id = await _seed_test_city(seed_conn, "test-cli-helper")
        finally:
            await seed_conn.close()

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

        web = await db_conn.fetchrow("SELECT id, url FROM websites WHERE maps_place_id = 'cli-pid'")
        assert web is not None
        assert web["url"] == "https://test-cli.example.com"

    async def test_run_discovery_cli_helper_no_cities(
        self, monkeypatch: pytest.MonkeyPatch, postgres_dsn: str
    ) -> None:
        """run_discovery() with API key but no pending cities."""
        _point_settings_at_fixture_db(monkeypatch, postgres_dsn)

        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            places_client = PlacesAPIClient(api_key="test")
            places_client.search_text = AsyncMock()
            places_client.close = AsyncMock()
            mock_client_cls.return_value = places_client

            result = await run_discovery(countries=["ZZ"])  # Non-existent country

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
