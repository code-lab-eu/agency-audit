"""Tests for the FastAPI + HTMX web dashboard (web/app.py).

Tests all routes, HTMX partials, API endpoint, template helpers, and query helpers.
Uses FastAPI TestClient against the real database (no DB-layer mocks).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from agency_audit.config import settings
from agency_audit.db import get_pool
from agency_audit.web.app import _score_color, _status_badge, app

# TestClient is module-level — each test reuses the same instance.
# It creates its own event loop per request via anyio.
client = TestClient(app)


# ──────────────────────────────────────────────────────────────────────
# Pool lifecycle: create once per test function, close after.
# Because TestClient uses its own event loop, we cannot share a pool
# between sync (TestClient) and async tests.  Each test gets a fresh
# pool on its own event loop by resetting the module-level singleton
# before and after every test.
# ──────────────────────────────────────────────────────────────────────


def _reset_pool() -> None:
    """Synchronously nuke the module-level pool reference.

    We do NOT call close_pool() because the pool may have been created
    on TestClient's anyio event loop which is already closed.  Instead
    we just drop the reference so the next get_pool() call creates a
    fresh pool on whatever event loop is current.
    """
    import agency_audit.db as _db

    _db._pool = None  # type: ignore[assignment]


async def _direct_conn() -> asyncpg.Connection:
    """Create a short-lived direct connection (commits immediately)."""
    return await asyncpg.connect(dsn=settings.dsn)


_cleanup_sql = """\
    DELETE FROM discovery_log;
    DELETE FROM website_cities;
    DELETE FROM websites WHERE url LIKE 'https://test-%';
    UPDATE cities SET discovery_status = 'pending';
"""


@pytest.fixture(autouse=True)
async def _reset_db() -> AsyncGenerator[None]:
    """Reset database state and pool before and after every test."""
    _reset_pool()

    conn = await _direct_conn()
    try:
        await conn.execute(_cleanup_sql)
    finally:
        await conn.close()
    yield
    conn = await _direct_conn()
    try:
        await conn.execute(_cleanup_sql)
    finally:
        await conn.close()
    _reset_pool()


# ──────────────────────────────────────────────────────────────────────
# Per-fixture seed helpers (clean up after themselves)
# ──────────────────────────────────────────────────────────────────────


async def _seed_city_42_sql(conn: asyncpg.Connection) -> None:
    """Insert test city 42 in Belgium (idempotent)."""
    await conn.execute(
        """\
        INSERT INTO cities (id, country, label, slug, population,
                            latitude, longitude, discovery_status)
        VALUES (42, 'BE', 'Brussels', 'brussels', 1000000,
                50.85, 4.35, 'pending')
        ON CONFLICT (id) DO UPDATE SET discovery_status = 'pending'\
        """
    )


async def _cleanup_city_42_sql(conn: asyncpg.Connection) -> None:
    """Remove city 42 and its website_cities links."""
    await conn.execute("DELETE FROM website_cities WHERE city_id = 42")
    await conn.execute("DELETE FROM cities WHERE id = 42")


@pytest.fixture
async def _seed_city_42() -> AsyncGenerator[None]:
    """Insert test city 42, clean up after test."""
    conn = await _direct_conn()
    try:
        await _seed_city_42_sql(conn)
    finally:
        await conn.close()
    yield
    conn = await _direct_conn()
    try:
        await _cleanup_city_42_sql(conn)
    finally:
        await conn.close()


@pytest.fixture
async def _city_42_in_progress(_seed_city_42: None) -> AsyncGenerator[None]:
    """Set city 42 to 'in_progress'."""
    conn = await _direct_conn()
    try:
        await conn.execute("UPDATE cities SET discovery_status = 'in_progress' WHERE id = 42")
    finally:
        await conn.close()
    yield


@pytest.fixture
async def _city_42_done(_seed_city_42: None) -> AsyncGenerator[None]:
    """Set city 42 to 'done'."""
    conn = await _direct_conn()
    try:
        await conn.execute("UPDATE cities SET discovery_status = 'done' WHERE id = 42")
    finally:
        await conn.close()
    yield


@pytest.fixture
async def _test_website_id() -> AsyncGenerator[int]:
    """Insert a test website, return its ID, clean up after."""
    conn = await _direct_conn()
    try:
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_data, audit_status)
            VALUES ('https://test-website.example.com', 'Test Agency', 75,
                    '{"score":75,"modules":{}}', 'audited')
            RETURNING id\
            """
        )
        assert row is not None
        yield row["id"]
    finally:
        await conn.close()


@pytest.fixture
async def _seed_bg_website() -> AsyncGenerator[int]:
    """Insert a website linked to Sofia (city 1), return its ID, clean up after."""
    conn = await _direct_conn()
    try:
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-bg.example.com', 'Example BG Agency', 42, 'audited')
            RETURNING id\
            """
        )
        website_id = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
            website_id,
        )
        yield website_id
    finally:
        await conn.close()


# ──────────────────────────────────────────────────────────────────────
# Template helpers (no database needed)
# ──────────────────────────────────────────────────────────────────────


def test_score_color_success() -> None:
    assert _score_color(50) == "text-success"
    assert _score_color(80) == "text-success"
    assert _score_color(100) == "text-success"


def test_score_color_warning() -> None:
    assert _score_color(20) == "text-warning"
    assert _score_color(49) == "text-warning"


def test_score_color_secondary() -> None:
    assert _score_color(0) == "text-secondary"
    assert _score_color(19) == "text-secondary"


def test_score_color_danger() -> None:
    assert _score_color(-10) == "text-danger"
    assert _score_color(-1) == "text-danger"


def test_status_badge_known() -> None:
    result = str(_status_badge("pending"))
    assert "bg-secondary" in result
    assert "Pending" in result

    result = str(_status_badge("audited"))
    assert "bg-success" in result
    assert "Audited" in result

    result = str(_status_badge("failed"))
    assert "bg-danger" in result
    assert "Failed" in result

    result = str(_status_badge("skipped"))
    assert "bg-warning" in result
    assert "Skipped" in result

    result = str(_status_badge("in_progress"))
    assert "bg-info" in result
    assert "In Progress" in result

    result = str(_status_badge("found"))
    assert "bg-primary" in result
    assert "Found" in result

    result = str(_status_badge("searched"))
    assert "bg-secondary" in result
    assert "Searched" in result


def test_status_badge_unknown() -> None:
    result = str(_status_badge("unknown_status"))
    assert "bg-secondary" in result
    assert "Unknown Status" in result


# ──────────────────────────────────────────────────────────────────────
# Route: / (overview)
# ──────────────────────────────────────────────────────────────────────


def test_overview_route_templates_exist() -> None:
    """Sanity check: the overview page renders with real (seeded) data."""
    response = client.get("/")
    assert response.status_code == 200
    assert "<html" in response.text.lower() or "DOCTYPE" in response.text


# ──────────────────────────────────────────────────────────────────────
# Route: /countries
# ──────────────────────────────────────────────────────────────────────


def test_countries_route() -> None:
    """Countries page renders with seeded active countries."""
    response = client.get("/countries")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Route: /country/{iso}
# ──────────────────────────────────────────────────────────────────────


def test_country_detail_route_found() -> None:
    """Country detail page for a seeded country (BG)."""
    response = client.get("/country/BG")
    assert response.status_code == 200


def test_country_detail_route_not_found() -> None:
    """Non-existent country returns 404."""
    response = client.get("/country/XX")
    assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /website/{website_id}
# ──────────────────────────────────────────────────────────────────────


def test_website_detail_route_found(_test_website_id: int) -> None:
    """Website detail page for an existing website."""
    response = client.get(f"/website/{_test_website_id}")
    assert response.status_code == 200


def test_website_detail_route_not_found() -> None:
    """Non-existent website returns 404."""
    response = client.get("/website/99999")
    assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /discovery
# ──────────────────────────────────────────────────────────────────────


def test_discovery_route() -> None:
    """Discovery queue page renders with seeded data."""
    response = client.get("/discovery")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX partials
# ──────────────────────────────────────────────────────────────────────


def test_htmx_stats() -> None:
    response = client.get("/htmx/stats")
    assert response.status_code == 200


def test_htmx_discovery_queue() -> None:
    response = client.get("/htmx/discovery/queue")
    assert response.status_code == 200


def test_htmx_rediscover_city() -> None:
    """POST /htmx/discovery/rediscover/1 resets a seeded city to 'pending'."""
    # City id=1 (Sofia) exists in the seed data.
    response = client.post("/htmx/discovery/rediscover/1")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX discover-city tests (keep non-DB mocks: settings, _run_city_discovery)
# ──────────────────────────────────────────────────────────────────────


def test_htmx_discover_city_triggers_background(_seed_city_42: None) -> None:
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        response = client.post("/htmx/country/BE/cities/42/discover")

    assert response.status_code == 200
    mock_run.assert_called_once()
    assert "every 3s" in response.text
    assert "spinner-border" in response.text


def test_htmx_discover_city_idempotent_when_already_running(
    _city_42_in_progress: None,
) -> None:
    """A second click while in_progress re-renders the row but enqueues nothing."""
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        response = client.post("/htmx/country/BE/cities/42/discover")

    assert response.status_code == 200
    mock_run.assert_not_called()
    assert "every 3s" in response.text
    assert "spinner-border" in response.text


def test_htmx_discover_city_country_mismatch_404(_seed_city_42: None) -> None:
    """A city that doesn't belong to the URL's country returns 404."""
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        response = client.post("/htmx/country/BG/cities/42/discover")

    # City 42 is in BE, not BG -> 404
    assert response.status_code == 404
    mock_run.assert_not_called()


def test_htmx_discover_city_requires_api_key(_seed_city_42: None) -> None:
    with patch("agency_audit.web.app.settings") as mock_settings:
        mock_settings.google_maps_api_key = ""

        response = client.post("/htmx/country/BE/cities/42/discover")

    assert response.status_code == 200
    assert "No Google Maps API key configured" in response.text


# ──────────────────────────────────────────────────────────────────────
# HTMX city row
# ──────────────────────────────────────────────────────────────────────


def test_htmx_city_row(_seed_city_42: None) -> None:
    response = client.get("/htmx/country/BE/cities/42/row")
    assert response.status_code == 200
    # City is pending -> shows refresh button, no polling spinner.
    assert "Refresh discovery" in response.text
    # Pending (not in_progress) triggers website table refresh.
    assert response.headers.get("HX-Trigger") == "discoveryComplete"


def test_htmx_city_row_done_triggers_refresh(_city_42_done: None) -> None:
    """When city is 'done', polling stops and HX-Trigger fires."""
    response = client.get("/htmx/country/BE/cities/42/row")
    assert response.status_code == 200
    assert "every 3s" not in response.text
    assert "Refresh discovery" in response.text
    assert response.headers.get("HX-Trigger") == "discoveryComplete"


# ──────────────────────────────────────────────────────────────────────
# HTMX country websites
# ──────────────────────────────────────────────────────────────────────


def test_htmx_country_websites(_seed_bg_website: int) -> None:
    response = client.get("/htmx/country/BG/websites")
    assert response.status_code == 200
    assert 'id="websites-table"' in response.text
    assert "discoveryComplete from:body" in response.text
    assert "Example BG Agency" in response.text


# ──────────────────────────────────────────────────────────────────────
# HTMX recent activity
# ──────────────────────────────────────────────────────────────────────


def test_htmx_recent_activity() -> None:
    """Recent activity renders (empty) with seeded data."""
    response = client.get("/htmx/recent-activity")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# API endpoint
# ──────────────────────────────────────────────────────────────────────


def test_api_stats() -> None:
    """JSON stats endpoint returns expected keys with real (seeded) data."""
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "countries" in data
    assert "cities_total" in data
    assert "websites_total" in data
    assert "avg_score" in data


# ──────────────────────────────────────────────────────────────────────
# Query helpers (direct testing against real database)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_stats() -> None:
    from agency_audit.web.app import _overview_stats

    pool = await get_pool()
    # Seed websites with known scores for distribution testing.
    async with pool.acquire() as conn:
        await conn.execute(
            """\
            INSERT INTO websites (url, label, score, audit_status) VALUES
                ('https://test-high.example.com', 'High', 80, 'audited'),
                ('https://test-mid.example.com', 'Mid', 30, 'audited'),
                ('https://test-low.example.com', 'Low', 10, 'audited'),
                ('https://test-neg.example.com', 'Neg', -5, 'audited'),
                ('https://test-pending.example.com', 'Pend', 0, 'pending')\
            """
        )

    stats = await _overview_stats(pool)

    # 4 active countries seeded
    assert stats["countries"] == 4
    # 20 cities seeded
    assert stats["cities_total"] == 20
    # 4 audited websites
    assert stats["websites_audited"] == 4
    # 5 total websites (4 audited + 1 pending)
    assert stats["websites_total"] == 5
    # Average of [80, 30, 10, -5] = 28.75
    assert stats["avg_score"] == 28.75

    # Score distribution
    buckets = {b["bucket"]: b["cnt"] for b in stats["score_distribution"]}
    assert buckets["50+"] == 1  # 80
    assert buckets["20-49"] == 1  # 30
    assert buckets["0-19"] == 1  # 10
    assert buckets["negative"] == 1  # -5


@pytest.mark.asyncio
async def test_country_list() -> None:
    from agency_audit.web.app import _country_list

    pool = await get_pool()

    result = await _country_list(pool)

    # 4 active countries: BE, BG, ES, RS
    assert len(result) == 4
    isos = {r["iso"] for r in result}
    assert isos == {"BE", "BG", "ES", "RS"}

    # BG has 20 seeded cities, others have none
    bg = [r for r in result if r["iso"] == "BG"][0]
    assert bg["city_count"] == 20
    assert bg["websites_discovered"] == 0  # no websites yet


@pytest.mark.asyncio
async def test_country_list_with_websites() -> None:
    """Country list with websites reflecting real JOIN aggregates."""
    from agency_audit.web.app import _country_list

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Insert website linked to Sofia (city 1, in BG)
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-list.example.com', 'List Agency', 60, 'audited')
            RETURNING id\
            """
        )
        website_id = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
            website_id,
        )

    result = await _country_list(pool)
    bg = [r for r in result if r["iso"] == "BG"][0]
    assert bg["websites_discovered"] == 1
    assert bg["websites_audited"] == 1
    assert float(bg["avg_score"]) == 60.0


@pytest.mark.asyncio
async def test_country_detail() -> None:
    from agency_audit.web.app import _country_detail

    pool = await get_pool()

    result = await _country_detail(pool, "BG")
    assert result is not None
    assert result["country"]["iso"] == "BG"
    assert "cities" in result
    assert "websites" in result
    # 20 seeded cities in BG
    assert len(result["cities"]) == 20


@pytest.mark.asyncio
async def test_country_detail_none() -> None:
    from agency_audit.web.app import _country_detail

    pool = await get_pool()

    result = await _country_detail(pool, "XX")
    assert result is None


@pytest.mark.asyncio
async def test_website_detail() -> None:
    from agency_audit.web.app import _website_detail

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_data, audit_status)
            VALUES ('https://test-detail.example.com', 'Test Detail', 80,
                    '{"score":80,"modules":{"robots":true}}', 'audited')
            RETURNING id\
            """
        )
        website_id = row["id"]
        # Link to Sofia (city 1)
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id, discovered_via) "
            "VALUES ($1, 1, 'google_maps')",
            website_id,
        )

    result = await _website_detail(pool, website_id)
    assert result is not None
    assert result["website"]["url"] == "https://test-detail.example.com"
    assert result["website"]["score"] == 80
    assert result["website"]["audit_status"] == "audited"
    assert isinstance(result["website"]["audit_data"], dict)
    assert result["website"]["audit_data"]["modules"]["robots"] is True
    assert "cities" in result
    assert len(result["cities"]) == 1
    assert result["cities"][0]["label"] == "Sofia"
    assert result["cities"][0]["discovered_via"] == "google_maps"
    assert "discovery_logs" in result


@pytest.mark.asyncio
async def test_website_detail_none() -> None:
    from agency_audit.web.app import _website_detail

    pool = await get_pool()

    result = await _website_detail(pool, 99999)
    assert result is None


@pytest.mark.asyncio
async def test_discovery_queue() -> None:
    from agency_audit.web.app import _discovery_queue

    pool = await get_pool()

    result = await _discovery_queue(pool)
    assert result["counts"]["total"] == 20  # 20 seeded cities
    assert result["counts"]["pending"] == 20  # all pending initially
    assert result["counts"]["done"] == 0
    assert result["counts"]["in_progress"] == 0
    assert result["counts"]["skipped"] == 0
    # Pending queue should have all 20 cities (LIMIT 50)
    assert len(result["pending"]) == 20


@pytest.mark.asyncio
async def test_recent_activity_empty() -> None:
    from agency_audit.web.app import _recent_activity

    pool = await get_pool()

    result = await _recent_activity(pool)
    assert result == []


@pytest.mark.asyncio
async def test_recent_activity_with_data() -> None:
    from agency_audit.web.app import _recent_activity

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Insert a website first (discovery_log references it)
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-activity.example.com', 'Test Activity', 0, 'pending')
            RETURNING id\
            """
        )
        website_id = row["id"]
        await conn.execute(
            """\
            INSERT INTO discovery_log (city_id, website_id, agent, search_query, status)
            VALUES (1, $1, 'google_maps', 'test query sofia', 'found')\
            """,
            website_id,
        )

    result = await _recent_activity(pool, limit=5)
    assert len(result) == 1
    assert result[0]["agent"] == "google_maps"
    assert result[0]["search_query"] == "test query sofia"
    assert result[0]["status"] == "found"
    assert result[0]["city_label"] == "Sofia"
    assert result[0]["website_label"] == "Test Activity"


# ──────────────────────────────────────────────────────────────────────
# _run_city_discovery error handling (keep DiscoveryPipeline mock)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_city_discovery_marks_failed_on_error() -> None:
    """A failing background discovery marks the city 'failed' so polling stops.

    'failed' must be a status the DB CHECK constraint accepts (migration 004),
    otherwise this UPDATE would itself raise and leave the row stuck
    'in_progress'.
    """
    with patch("agency_audit.discovery.DiscoveryPipeline") as mock_pipeline_cls:
        pipeline = mock_pipeline_cls.return_value
        pipeline.discover_city = AsyncMock(side_effect=RuntimeError("boom"))
        pipeline.close = AsyncMock()

        from agency_audit.web.app import _run_city_discovery

        await _run_city_discovery(1, "BG")

        # Discovery uses the city's stored country, not a caller-supplied ISO.
        assert pipeline.discover_city.call_args.kwargs["country_iso"] == "BG"

        # Verify city 1 was marked 'failed' in the real database.
        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchval("SELECT discovery_status FROM cities WHERE id = 1")
        assert status == "failed"

        pipeline.close.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# Health endpoint
# ──────────────────────────────────────────────────────────────────────


def test_health_healthy() -> None:
    """Health endpoint returns 200 when database is reachable."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["db"] == "connected"


def test_health_unhealthy() -> None:
    """Health endpoint returns 503 when database is unreachable."""
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:  # db-mock-check: ignore
        mock_get_pool.side_effect = RuntimeError("connection refused")

        response = client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["db"] == "disconnected"
    assert "connection refused" in data["detail"]


# ──────────────────────────────────────────────────────────────────────
# Migration 004 verification (no database needed)
# ──────────────────────────────────────────────────────────────────────


def test_migration_004_allows_failed_status() -> None:
    """The 'failed' status the failure path writes must be in the CHECK constraint."""
    from pathlib import Path

    import agency_audit.migrations as migrations_pkg

    sql = (Path(migrations_pkg.__file__).parent / "004_add_failed_discovery_status.sql").read_text(
        encoding="utf-8"
    )
    assert "'failed'" in sql
    assert "cities_discovery_status_check" in sql
