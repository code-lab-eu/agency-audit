"""Tests for the FastAPI + HTMX web dashboard (web/app.py).

Tests all routes, HTMX partials, API endpoint, template helpers, and query helpers.
Uses FastAPI TestClient against the real database (no DB-layer mocks).

The ``postgres_dsn`` fixture (conftest.py) provisions a disposable test database;
settings monkeypatching routes both direct connections and the web app's
``get_pool()`` to the same DSN.  ``close_pool()`` is called in teardown to avoid
leaking connections — it first tries the proper async close, and falls back to
resetting the module-level reference when the pool was created on TestClient's
anyio event loop (which may already be closed).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import asyncpg
import pytest
from fastapi.testclient import TestClient

from agency_audit.config import settings
from agency_audit.db import close_pool
from agency_audit.web.app import _score_color, _status_badge, app

# ──────────────────────────────────────────────────────────────────────
# Client fixture — ensures the FastAPI app uses the test database
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient wired to the disposable test database.

    Monkeypatches ``agency_audit.config.settings`` so every
    ``get_pool()`` call inside the web app connects to the test
    database, not the ambient one.
    """
    parsed = urlparse(postgres_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
    monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")
    return TestClient(app)


# ──────────────────────────────────────────────────────────────────────
# Pool cleanup — close pools properly instead of dropping references
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _close_pool_after_test() -> AsyncGenerator[None]:
    """Close the shared pool after every test so no connections leak.

    For async tests the pool is created on pytest's event loop and
    ``close_pool()`` works directly.  For sync tests that use
    ``TestClient``, the pool may live on anyio's event loop — in that
    case ``close_pool()`` raises and we fall back to resetting the
    module-level reference so the next test starts fresh.

    Also cleans up test-* websites so data from one test never leaks
    into another (the ``_app_pool`` fixture uses a committed pool).
    """
    yield
    try:
        # Clean up test data left by committed-pool tests.
        from agency_audit.db import get_pool as _gp

        pool = await _gp()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM discovery_log WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await conn.execute(
                "DELETE FROM website_cities WHERE website_id IN "
                "(SELECT id FROM websites WHERE url LIKE 'https://test-%')"
            )
            await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")
        await close_pool()
    except Exception:
        # Pool was created on TestClient's anyio loop which is already
        # closed.  Reset the reference so the next get_pool() starts clean.
        import agency_audit.db as _db

        _db._pool = None  # type: ignore[assignment]
        _db._pool_closed = False


# ──────────────────────────────────────────────────────────────────────
# Seeding helpers — committed connections so the web app's pool sees them
# ──────────────────────────────────────────────────────────────────────


async def _committed_conn(postgres_dsn: str) -> asyncpg.Connection:
    """Open a short-lived connection that auto-commits immediately."""
    return await asyncpg.connect(dsn=postgres_dsn)


@pytest.fixture
async def _test_website(postgres_dsn: str) -> AsyncGenerator[int]:
    """Insert a test website on a committed connection, clean up after."""
    conn = await _committed_conn(postgres_dsn)
    try:
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_data, audit_status)
            VALUES ('https://test-website.example.com', 'Test Agency', 75,
                    '{"score":75,"modules":{}}', 'audited')
            RETURNING id\
            """
        )
        website_id: int = row["id"]
        yield website_id
        await conn.execute("DELETE FROM websites WHERE id = $1", website_id)
    finally:
        await conn.close()


@pytest.fixture
async def _seed_bg_website(postgres_dsn: str) -> AsyncGenerator[int]:
    """Insert a test website linked to Sofia (city 1), clean up after."""
    conn = await _committed_conn(postgres_dsn)
    try:
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-bg.example.com', 'Example BG Agency', 42, 'audited')
            RETURNING id\
            """
        )
        website_id: int = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, 1)",
            website_id,
        )
        yield website_id
        await conn.execute("DELETE FROM website_cities WHERE website_id = $1", website_id)
        await conn.execute("DELETE FROM websites WHERE id = $1", website_id)
    finally:
        await conn.close()


@pytest.fixture
async def _seed_city_42(postgres_dsn: str) -> AsyncGenerator[None]:
    """Insert test city 42 (Brussels, BE) on a committed connection."""
    conn = await _committed_conn(postgres_dsn)
    try:
        await conn.execute(
            """\
            INSERT INTO cities (id, country, label, slug, population,
                                latitude, longitude, discovery_status)
            VALUES (42, 'BE', 'Brussels', 'brussels', 1000000,
                    50.85, 4.35, 'pending')
            ON CONFLICT (id) DO UPDATE SET discovery_status = 'pending'\
            """
        )
        yield
        await conn.execute("DELETE FROM website_cities WHERE city_id = 42")
        await conn.execute("DELETE FROM cities WHERE id = 42")
    finally:
        await conn.close()


@pytest.fixture
async def _city_42_in_progress(_seed_city_42: None, postgres_dsn: str) -> AsyncGenerator[None]:
    """Set city 42 discovery_status to 'in_progress'."""
    conn = await _committed_conn(postgres_dsn)
    try:
        await conn.execute("UPDATE cities SET discovery_status = 'in_progress' WHERE id = 42")
        yield
    finally:
        await conn.close()


@pytest.fixture
async def _city_42_done(_seed_city_42: None, postgres_dsn: str) -> AsyncGenerator[None]:
    """Set city 42 discovery_status to 'done'."""
    conn = await _committed_conn(postgres_dsn)
    try:
        await conn.execute("UPDATE cities SET discovery_status = 'done' WHERE id = 42")
        yield
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


def test_overview_route_templates_exist(client: TestClient) -> None:
    """Sanity check: the overview page renders with real (seeded) data."""
    response = client.get("/")
    assert response.status_code == 200
    assert "<html" in response.text.lower() or "DOCTYPE" in response.text


# ──────────────────────────────────────────────────────────────────────
# Route: /countries
# ──────────────────────────────────────────────────────────────────────


def test_countries_route(client: TestClient) -> None:
    """Countries page renders with seeded active countries."""
    response = client.get("/countries")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Route: /country/{iso}
# ──────────────────────────────────────────────────────────────────────


def test_country_detail_route_found(client: TestClient) -> None:
    """Country detail page for a seeded country (BG)."""
    response = client.get("/country/BG")
    assert response.status_code == 200


def test_country_detail_route_not_found(client: TestClient) -> None:
    """Non-existent country returns 404."""
    response = client.get("/country/XX")
    assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /website/{website_id}
# ──────────────────────────────────────────────────────────────────────


def test_website_detail_route_found(client: TestClient, _test_website: int) -> None:
    """Website detail page for an existing website."""
    response = client.get(f"/website/{_test_website}")
    assert response.status_code == 200


def test_website_detail_route_not_found(client: TestClient) -> None:
    """Non-existent website returns 404."""
    response = client.get("/website/99999")
    assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /discovery
# ──────────────────────────────────────────────────────────────────────


def test_discovery_route(client: TestClient) -> None:
    """Discovery queue page renders with seeded data."""
    response = client.get("/discovery")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX partials
# ──────────────────────────────────────────────────────────────────────


def test_htmx_stats(client: TestClient) -> None:
    response = client.get("/htmx/stats")
    assert response.status_code == 200


def test_htmx_discovery_queue(client: TestClient) -> None:
    response = client.get("/htmx/discovery/queue")
    assert response.status_code == 200


def test_htmx_rediscover_city(client: TestClient) -> None:
    """POST /htmx/discovery/rediscover/1 resets a seeded city to 'pending'."""
    # City id=1 (Sofia) exists in the seed data.
    response = client.post("/htmx/discovery/rediscover/1")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX discover-city tests (keep non-DB mocks: settings, _run_city_discovery)
# ──────────────────────────────────────────────────────────────────────


def test_htmx_discover_city_triggers_background(client: TestClient, _seed_city_42: None) -> None:
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
    client: TestClient, _city_42_in_progress: None
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


def test_htmx_discover_city_country_mismatch_404(client: TestClient, _seed_city_42: None) -> None:
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


def test_htmx_discover_city_requires_api_key(client: TestClient, _seed_city_42: None) -> None:
    with patch("agency_audit.web.app.settings") as mock_settings:
        mock_settings.google_maps_api_key = ""

        response = client.post("/htmx/country/BE/cities/42/discover")

    assert response.status_code == 200
    assert "No Google Maps API key configured" in response.text


# ──────────────────────────────────────────────────────────────────────
# HTMX city row
# ──────────────────────────────────────────────────────────────────────


def test_htmx_city_row(client: TestClient, _seed_city_42: None) -> None:
    response = client.get("/htmx/country/BE/cities/42/row")
    assert response.status_code == 200
    # City is pending -> shows refresh button, no polling spinner.
    assert "Refresh discovery" in response.text
    # Pending (not in_progress) triggers website table refresh.
    assert response.headers.get("HX-Trigger") == "discoveryComplete"


def test_htmx_city_row_done_triggers_refresh(client: TestClient, _city_42_done: None) -> None:
    """When city is 'done', polling stops and HX-Trigger fires."""
    response = client.get("/htmx/country/BE/cities/42/row")
    assert response.status_code == 200
    assert "every 3s" not in response.text
    assert "Refresh discovery" in response.text
    assert response.headers.get("HX-Trigger") == "discoveryComplete"


# ──────────────────────────────────────────────────────────────────────
# HTMX country websites
# ──────────────────────────────────────────────────────────────────────


def test_htmx_country_websites(client: TestClient, _seed_bg_website: int) -> None:
    response = client.get("/htmx/country/BG/websites")
    assert response.status_code == 200
    assert 'id="websites-table"' in response.text
    assert "discoveryComplete from:body" in response.text
    assert "Example BG Agency" in response.text


# ──────────────────────────────────────────────────────────────────────
# HTMX recent activity
# ──────────────────────────────────────────────────────────────────────


def test_htmx_recent_activity(client: TestClient) -> None:
    """Recent activity renders (empty) with seeded data."""
    response = client.get("/htmx/recent-activity")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# API endpoint
# ──────────────────────────────────────────────────────────────────────


def test_api_stats(client: TestClient) -> None:
    """JSON stats endpoint returns expected keys with real (seeded) data."""
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "countries" in data
    assert "cities_total" in data
    assert "websites_total" in data
    assert "avg_score" in data


# ──────────────────────────────────────────────────────────────────────
# Query helper integration tests — async, against the real database
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
async def _app_pool(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> asyncpg.Pool:
    """Pool for the web app's query helpers, bound to the test database."""
    parsed = urlparse(postgres_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
    monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")
    from agency_audit.db import get_pool

    return await get_pool()


@pytest.mark.asyncio
async def test_overview_stats(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _overview_stats

    # Seed websites with known scores for distribution testing.
    async with _app_pool.acquire() as conn:
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

    stats = await _overview_stats(_app_pool)

    # 4 active countries seeded
    assert stats["countries"] == 4
    # 20 cities seeded
    assert stats["cities_total"] == 20
    # 4 audited websites (the ones we just seeded)
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

    # Clean up so the next test sees a clean slate.
    async with _app_pool.acquire() as conn:
        await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")


@pytest.mark.asyncio
async def test_country_list(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _country_list

    result = await _country_list(_app_pool)

    # 4 active countries: BE, BG, ES, RS
    assert len(result) == 4
    isos = {r["iso"] for r in result}
    assert isos == {"BE", "BG", "ES", "RS"}

    # BG has 20 seeded cities, others have none
    bg = [r for r in result if r["iso"] == "BG"][0]
    assert bg["city_count"] == 20
    assert bg["websites_discovered"] == 0  # no websites yet


@pytest.mark.asyncio
async def test_country_list_with_websites(_app_pool: asyncpg.Pool) -> None:
    """Country list with websites reflecting real JOIN aggregates."""
    from agency_audit.web.app import _country_list

    async with _app_pool.acquire() as conn:
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

    result = await _country_list(_app_pool)
    bg = [r for r in result if r["iso"] == "BG"][0]
    assert bg["websites_discovered"] == 1
    assert bg["websites_audited"] == 1
    assert float(bg["avg_score"]) == 60.0

    # Clean up
    async with _app_pool.acquire() as conn:
        await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")


@pytest.mark.asyncio
async def test_country_detail(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _country_detail

    result = await _country_detail(_app_pool, "BG")
    assert result is not None
    assert result["country"]["iso"] == "BG"
    assert "cities" in result
    assert "websites" in result
    # 20 seeded cities in BG
    assert len(result["cities"]) == 20


@pytest.mark.asyncio
async def test_country_detail_none(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _country_detail

    result = await _country_detail(_app_pool, "XX")
    assert result is None


@pytest.mark.asyncio
async def test_website_detail(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _website_detail

    async with _app_pool.acquire() as conn:
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

    result = await _website_detail(_app_pool, website_id)
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

    # Clean up
    async with _app_pool.acquire() as conn:
        await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")


@pytest.mark.asyncio
async def test_website_detail_none(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _website_detail

    result = await _website_detail(_app_pool, 99999)
    assert result is None


@pytest.mark.asyncio
async def test_discovery_queue(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _discovery_queue

    result = await _discovery_queue(_app_pool)
    assert result["counts"]["total"] == 20  # 20 seeded cities
    assert result["counts"]["pending"] == 20  # all pending initially
    assert result["counts"]["done"] == 0
    assert result["counts"]["in_progress"] == 0
    assert result["counts"]["skipped"] == 0
    # Pending queue should have all 20 cities (LIMIT 50)
    assert len(result["pending"]) == 20


@pytest.mark.asyncio
async def test_recent_activity_empty(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _recent_activity

    result = await _recent_activity(_app_pool)
    # No test-* entries from previous tests (cleanup fixture handles that).
    # We don't assert on exact count — other test files may have written
    # discovery_log rows.
    urls: set[str] = set()
    for row in result:
        if row.get("website_url"):
            urls.add(row["website_url"])
    assert not any(u.startswith("https://test-") for u in urls), (
        f"Found test-* website URLs in results: {urls}"
    )


@pytest.mark.asyncio
async def test_recent_activity_with_data(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _recent_activity

    async with _app_pool.acquire() as conn:
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

    result = await _recent_activity(_app_pool, limit=50)  # ensure our entry is included
    # Our specific entry must be present (but other entries from different tests may exist too).
    our_entry = [
        r
        for r in result
        if r["search_query"] == "test query sofia" and r["website_label"] == "Test Activity"
    ]
    assert len(our_entry) == 1, f"Expected 1 matching entry, found {len(our_entry)}"
    assert our_entry[0]["agent"] == "google_maps"
    assert our_entry[0]["status"] == "found"
    assert our_entry[0]["city_label"] == "Sofia"
    assert our_entry[0]["website_url"] == "https://test-activity.example.com"

    # Clean up
    async with _app_pool.acquire() as conn:
        await conn.execute("DELETE FROM discovery_log WHERE search_query = 'test query sofia'")
        await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")


# ──────────────────────────────────────────────────────────────────────
# _run_city_discovery error handling (keep DiscoveryPipeline mock)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_city_discovery_marks_failed_on_error(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing background discovery marks the city 'failed' so polling stops.

    'failed' must be a status the DB CHECK constraint accepts (migration 004),
    otherwise this UPDATE would itself raise and leave the row stuck
    'in_progress'.
    """
    # Point get_pool() at the test database so _run_city_discovery's
    # internal pool connects here.
    parsed = urlparse(postgres_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", (parsed.path or "/agency_audit").lstrip("/"))
    monkeypatch.setattr(settings, "pg_user", parsed.username or "agency_audit")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")

    with patch("agency_audit.discovery.DiscoveryPipeline") as mock_pipeline_cls:
        pipeline = mock_pipeline_cls.return_value
        pipeline.discover_city = AsyncMock(side_effect=RuntimeError("boom"))
        pipeline.close = AsyncMock()

        from agency_audit.web.app import _run_city_discovery

        await _run_city_discovery(1, "BG")

        # Discovery uses the city's stored country, not a caller-supplied ISO.
        assert pipeline.discover_city.call_args.kwargs["country_iso"] == "BG"

        # Verify city 1 was marked 'failed' in the real database.
        from agency_audit.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchval("SELECT discovery_status FROM cities WHERE id = 1")
        assert status == "failed"

        pipeline.close.assert_awaited_once()

    # Reset city 1 so other tests aren't affected
    from agency_audit.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cities SET discovery_status = 'pending' WHERE id = 1")


# ──────────────────────────────────────────────────────────────────────
# Health endpoint
# ──────────────────────────────────────────────────────────────────────


def test_health_healthy(client: TestClient) -> None:
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

        response = TestClient(app).get("/health")
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
