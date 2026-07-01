"""Tests for the FastAPI + HTMX web dashboard (web/app.py).

Tests all routes, HTMX partials, API endpoint, template helpers, and query helpers.
Uses FastAPI TestClient against the real database (no DB-layer mocks).

Baseline seed data (countries + 20 BG cities) is loaded by the session-scoped
``_ensure_seed_data`` fixture in ``conftest.py``.  Tests insert their own
websites / discovery_log rows and query actual city IDs from the database
instead of hard-coding primary keys.
"""

from __future__ import annotations

import asyncio
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
def client(
    postgres_dsn: str, _ensure_seed_data: None, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
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
            # Clean up test-* cities and their website_cities links too.
            await conn.execute(
                "DELETE FROM website_cities WHERE city_id IN "
                "(SELECT id FROM cities WHERE slug LIKE 'test-%')"
            )
            await conn.execute("DELETE FROM cities WHERE slug LIKE 'test-%'")
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


# ── Dynamic city-ID lookups for sync fixtures (run asyncio once) ──────


def _run_async(coro):
    """Run a coroutine in a fresh event loop, returning the result."""
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def sofia_city_id(postgres_dsn: str) -> int:
    """Sofia's actual primary key, resolved once per test module."""

    async def _get():
        conn = await _committed_conn(postgres_dsn)
        try:
            sid = await conn.fetchval(
                "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
            )
        finally:
            await conn.close()
        assert sid is not None, "Sofia must exist in the test database"
        return sid

    return _run_async(_get())


# ── Test-fixture factories (committed inserts) ────────────────────────


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
    """Insert a test website linked to Sofia (dynamically resolved city ID)."""
    conn = await _committed_conn(postgres_dsn)
    try:
        city_id = await conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert city_id is not None, "Sofia must exist in the test database"
        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-bg.example.com', 'Example BG Agency', 42, 'audited')
            RETURNING id\
            """
        )
        website_id: int = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            city_id,
        )
        yield website_id
        await conn.execute("DELETE FROM website_cities WHERE website_id = $1", website_id)
        await conn.execute("DELETE FROM websites WHERE id = $1", website_id)
    finally:
        await conn.close()


@pytest.fixture
async def _test_brussels_city(postgres_dsn: str) -> AsyncGenerator[int]:
    """Insert a test Brussels city (committed) and return its actual ID."""
    conn = await _committed_conn(postgres_dsn)
    try:
        city_id = await conn.fetchval(
            """\
            INSERT INTO cities (country, label, slug, population,
                                latitude, longitude, discovery_status)
            VALUES ('BE', 'Brussels (test)', 'test-brussels',
                    1000000, 50.85, 4.35, 'pending')
            ON CONFLICT (country, slug) DO UPDATE SET discovery_status = 'pending'
            RETURNING id
            """
        )
        assert city_id is not None, "INSERT ... RETURNING id must return a value"
        yield city_id
        await conn.execute("DELETE FROM website_cities WHERE city_id = $1", city_id)
        await conn.execute("DELETE FROM cities WHERE id = $1", city_id)
    finally:
        await conn.close()


@pytest.fixture
async def _test_city_in_progress(
    _test_brussels_city: int, postgres_dsn: str
) -> AsyncGenerator[int]:
    """Set the test Brussels city to 'in_progress' (committed)."""
    conn = await _committed_conn(postgres_dsn)
    try:
        await conn.execute(
            "UPDATE cities SET discovery_status = 'in_progress' WHERE id = $1",
            _test_brussels_city,
        )
        yield _test_brussels_city
    finally:
        await conn.close()


@pytest.fixture
async def _test_city_done(_test_brussels_city: int, postgres_dsn: str) -> AsyncGenerator[int]:
    """Set the test Brussels city to 'done' (committed)."""
    conn = await _committed_conn(postgres_dsn)
    try:
        await conn.execute(
            "UPDATE cities SET discovery_status = 'done' WHERE id = $1",
            _test_brussels_city,
        )
        yield _test_brussels_city
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


def test_htmx_rediscover_city(client: TestClient, sofia_city_id: int) -> None:
    """POST /htmx/discovery/rediscover/<id> resets the city to 'pending'."""
    response = client.post(f"/htmx/discovery/rediscover/{sofia_city_id}")
    assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX discover-city tests (keep non-DB mocks: settings, _run_city_discovery)
# ──────────────────────────────────────────────────────────────────────


def test_htmx_discover_city_triggers_background(
    client: TestClient, _test_brussels_city: int
) -> None:
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        response = client.post(f"/htmx/country/BE/cities/{_test_brussels_city}/discover")

    assert response.status_code == 200
    mock_run.assert_called_once()
    assert "every 3s" in response.text
    assert "spinner-border" in response.text


def test_htmx_discover_city_idempotent_when_already_running(
    client: TestClient, _test_city_in_progress: int
) -> None:
    """A second click while in_progress re-renders the row but enqueues nothing."""
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        response = client.post(f"/htmx/country/BE/cities/{_test_city_in_progress}/discover")

    assert response.status_code == 200
    mock_run.assert_not_called()
    assert "every 3s" in response.text
    assert "spinner-border" in response.text


def test_htmx_discover_city_country_mismatch_404(
    client: TestClient, _test_brussels_city: int
) -> None:
    """A city that doesn't belong to the URL's country returns 404."""
    with (
        patch("agency_audit.web.app.settings") as mock_settings,
        patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()) as mock_run,
    ):
        mock_settings.google_maps_api_key = "test-key"

        # City is in BE, not BG -> 404
        response = client.post(f"/htmx/country/BG/cities/{_test_brussels_city}/discover")

    assert response.status_code == 404
    mock_run.assert_not_called()


def test_htmx_discover_city_requires_api_key(client: TestClient, _test_brussels_city: int) -> None:
    with patch("agency_audit.web.app.settings") as mock_settings:
        mock_settings.google_maps_api_key = ""

        response = client.post(f"/htmx/country/BE/cities/{_test_brussels_city}/discover")

    assert response.status_code == 200
    assert "No Google Maps API key configured" in response.text


# ──────────────────────────────────────────────────────────────────────
# HTMX city row
# ──────────────────────────────────────────────────────────────────────


def test_htmx_city_row(client: TestClient, _test_brussels_city: int) -> None:
    response = client.get(f"/htmx/country/BE/cities/{_test_brussels_city}/row")
    assert response.status_code == 200
    # City is pending -> shows refresh button, no polling spinner.
    assert "Refresh discovery" in response.text
    # Pending (not in_progress) triggers website table refresh.
    assert response.headers.get("HX-Trigger") == "discoveryComplete"


def test_htmx_city_row_done_triggers_refresh(client: TestClient, _test_city_done: int) -> None:
    """When city is 'done', polling stops and HX-Trigger fires."""
    response = client.get(f"/htmx/country/BE/cities/{_test_city_done}/row")
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


# ── Helper: read baseline counts from the seeded database ─────────────


async def _baseline(pool: asyncpg.Pool) -> dict[str, int]:
    """Return counts of the baseline seed data so tests can compute deltas."""
    async with pool.acquire() as conn:
        return {
            "active_countries": await conn.fetchval("SELECT COUNT(*) FROM countries WHERE active"),
            "total_cities": await conn.fetchval("SELECT COUNT(*) FROM cities"),
            "total_websites": await conn.fetchval("SELECT COUNT(*) FROM websites"),
            "audited_websites": await conn.fetchval(
                "SELECT COUNT(*) FROM websites WHERE audit_status = 'audited'"
            ),
            "pending_cities": await conn.fetchval(
                "SELECT COUNT(*) FROM cities WHERE discovery_status = 'pending'"
            ),
            "total_discovery_log": await conn.fetchval("SELECT COUNT(*) FROM discovery_log"),
        }


@pytest.mark.asyncio
async def test_overview_stats(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _overview_stats

    base = await _baseline(_app_pool)

    # Seed test websites with known scores for distribution testing.
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

    # Assert against the baseline + our inserts.
    assert stats["countries"] == base["active_countries"]
    assert stats["cities_total"] == base["total_cities"]
    # 4 audited websites added + existing audited
    assert stats["websites_audited"] == base["audited_websites"] + 4
    # 5 total websites (4 audited + 1 pending) + existing
    assert stats["websites_total"] == base["total_websites"] + 5

    # Score distribution — only our 4 audited test sites matter for the
    # per-bucket assertions.  Grab the buckets and look for the values
    # that match our known scores.
    buckets = {b["bucket"]: b["cnt"] for b in stats["score_distribution"]}
    assert buckets["50+"] >= 1  # 80 from our insert
    assert buckets["20-49"] >= 1  # 30
    assert buckets["0-19"] >= 1  # 10
    assert buckets["negative"] >= 1  # -5

    # Clean up so the next test sees a clean slate.
    async with _app_pool.acquire() as conn:
        await conn.execute("DELETE FROM websites WHERE url LIKE 'https://test-%'")


@pytest.mark.asyncio
async def test_country_list(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _country_list

    base = await _baseline(_app_pool)
    result = await _country_list(_app_pool)

    # Should have exactly the seeded active countries.
    assert len(result) == base["active_countries"]
    isos = {r["iso"] for r in result}
    assert isos == {"BE", "BG", "ES", "RS"}

    # BG has the 20 seeded cities.
    bg = [r for r in result if r["iso"] == "BG"][0]
    assert bg["city_count"] == base["total_cities"]
    assert bg["websites_discovered"] == 0  # no websites yet


@pytest.mark.asyncio
async def test_country_list_with_websites(_app_pool: asyncpg.Pool) -> None:
    """Country list with websites reflecting real JOIN aggregates."""
    from agency_audit.web.app import _country_list

    async with _app_pool.acquire() as conn:
        # Resolve Sofia's actual ID.
        sofia_id = await conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_status)
            VALUES ('https://test-list.example.com', 'List Agency', 60, 'audited')
            RETURNING id\
            """
        )
        website_id = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            sofia_id,
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

    base = await _baseline(_app_pool)
    result = await _country_detail(_app_pool, "BG")
    assert result is not None
    assert result["country"]["iso"] == "BG"
    assert "cities" in result
    assert "websites" in result
    assert len(result["cities"]) == base["total_cities"]


@pytest.mark.asyncio
async def test_country_detail_none(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _country_detail

    result = await _country_detail(_app_pool, "XX")
    assert result is None


@pytest.mark.asyncio
async def test_website_detail(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _website_detail

    async with _app_pool.acquire() as conn:
        # Resolve Sofia's actual ID.
        sofia_id = await conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

        row = await conn.fetchrow(
            """\
            INSERT INTO websites (url, label, score, audit_data, audit_status)
            VALUES ('https://test-detail.example.com', 'Test Detail', 80,
                    '{"score":80,"modules":{"robots":true}}', 'audited')
            RETURNING id\
            """
        )
        website_id = row["id"]
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id, discovered_via) "
            "VALUES ($1, $2, 'google_maps')",
            website_id,
            sofia_id,
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

    base = await _baseline(_app_pool)
    result = await _discovery_queue(_app_pool)
    assert result["counts"]["total"] == base["total_cities"]
    assert result["counts"]["pending"] == base["pending_cities"]
    assert result["counts"]["done"] == 0
    assert result["counts"]["in_progress"] == 0
    assert result["counts"]["skipped"] == 0
    # Pending queue should have all cities (LIMIT 50).
    assert len(result["pending"]) == base["pending_cities"]


@pytest.mark.asyncio
async def test_recent_activity_empty(_app_pool: asyncpg.Pool) -> None:
    from agency_audit.web.app import _recent_activity

    result = await _recent_activity(_app_pool)
    # No test-* entries from previous tests (cleanup fixture handles that).
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
        # Resolve Sofia's actual ID.
        sofia_id = await conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
        assert sofia_id is not None

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
            VALUES ($1, $2, 'google_maps', 'test query sofia', 'found')\
            """,
            sofia_id,
            website_id,
        )

    result = await _recent_activity(_app_pool, limit=50)
    # Our specific entry must be present
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

    # Resolve Sofia's city ID dynamically.
    tmp_conn = await asyncpg.connect(dsn=postgres_dsn)
    try:
        sofia_id = await tmp_conn.fetchval(
            "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
        )
    finally:
        await tmp_conn.close()
    assert sofia_id is not None, "Sofia must exist in the test database"

    with patch("agency_audit.discovery.DiscoveryPipeline") as mock_pipeline_cls:
        pipeline = mock_pipeline_cls.return_value
        pipeline.discover_city = AsyncMock(side_effect=RuntimeError("boom"))
        pipeline.close = AsyncMock()

        from agency_audit.web.app import _run_city_discovery

        await _run_city_discovery(sofia_id, "BG")

        # Discovery uses the city's stored country, not a caller-supplied ISO.
        assert pipeline.discover_city.call_args.kwargs["country_iso"] == "BG"

        # Verify the city was marked 'failed' in the real database.
        from agency_audit.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", sofia_id
            )
        assert status == "failed"

        pipeline.close.assert_awaited_once()

    # Reset the city so other tests aren't affected.
    from agency_audit.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cities SET discovery_status = 'pending' WHERE id = $1", sofia_id)


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
