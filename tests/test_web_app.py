"""Tests for the FastAPI + HTMX web dashboard (web/app.py).

Tests all routes, HTMX partials, API endpoint, template helpers, and query helpers.
Uses FastAPI TestClient against the real database (no DB-layer mocks).

Each test runs against a private, pristine database provided by the
``fresh_db`` fixture (conftest.py): the canonical countries + 20 BG cities
seed is present, the mutable tables start empty, and the database is dropped
on teardown.  Tests seed their own websites / discovery_log rows and query
actual city IDs instead of hard-coding primary keys; no manual cleanup is
needed and exact-count assertions are safe.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from fastapi.testclient import TestClient

from agency_audit.config import settings
from agency_audit.web.app import _score_color, _status_badge, app

# ──────────────────────────────────────────────────────────────────────
# Client fixture — the FastAPI app runs against the per-test database
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(fresh_db: asyncpg.Connection) -> TestClient:
    """TestClient wired to the per-test database.

    Depending on ``fresh_db`` redirects ``get_pool()`` onto this test's
    private database and drops it (with its pool) afterwards, so the app
    reads and writes an isolated, pristine dataset.
    """
    return TestClient(app)


# ──────────────────────────────────────────────────────────────────────
# Seeding helpers — write on fresh_db's connection (committed, autocommit)
# so the web app's own pool sees the rows.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
async def sofia_city_id(fresh_db: asyncpg.Connection) -> int:
    """Sofia's primary key in this test's database."""
    sid: int | None = await fresh_db.fetchval(
        "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
    )
    assert sid is not None, "Sofia must exist in the seeded database"
    return sid


@pytest.fixture
async def _test_website(fresh_db: asyncpg.Connection) -> int:
    """Insert a test website and return its id."""
    row = await fresh_db.fetchrow(
        """\
        INSERT INTO websites (url, label, score, audit_data, audit_status)
        VALUES ('https://test-website.example.com', 'Test Agency', 75,
                '{"score":75,"modules":{}}', 'audited')
        RETURNING id\
        """
    )
    website_id: int = row["id"]
    return website_id


@pytest.fixture
async def _seed_bg_website(fresh_db: asyncpg.Connection) -> int:
    """Insert a test website linked to Sofia and return its id."""
    city_id = await fresh_db.fetchval(
        "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
    )
    assert city_id is not None, "Sofia must exist in the seeded database"
    row = await fresh_db.fetchrow(
        """\
        INSERT INTO websites (url, label, score, audit_status)
        VALUES ('https://test-bg.example.com', 'Example BG Agency', 42, 'audited')
        RETURNING id\
        """
    )
    website_id: int = row["id"]
    await fresh_db.execute(
        "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
        website_id,
        city_id,
    )
    return website_id


@pytest.fixture
async def _test_brussels_city(fresh_db: asyncpg.Connection) -> int:
    """Insert a test Brussels (BE) city and return its id."""
    city_id: int | None = await fresh_db.fetchval(
        """\
        INSERT INTO cities (country, label, slug, population,
                            latitude, longitude, discovery_status)
        VALUES ('BE', 'Brussels (test)', 'test-brussels',
                1000000, 50.85, 4.35, 'pending')
        RETURNING id
        """
    )
    assert city_id is not None, "INSERT ... RETURNING id must return a value"
    return city_id


@pytest.fixture
async def _test_city_in_progress(fresh_db: asyncpg.Connection, _test_brussels_city: int) -> int:
    """Set the test Brussels city to 'in_progress'."""
    await fresh_db.execute(
        "UPDATE cities SET discovery_status = 'in_progress' WHERE id = $1",
        _test_brussels_city,
    )
    return _test_brussels_city


@pytest.fixture
async def _test_city_done(fresh_db: asyncpg.Connection, _test_brussels_city: int) -> int:
    """Set the test Brussels city to 'done'."""
    await fresh_db.execute(
        "UPDATE cities SET discovery_status = 'done' WHERE id = $1",
        _test_brussels_city,
    )
    return _test_brussels_city


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

    # City status must remain 'pending' — the guard rejected the request
    # before it could transition to 'in_progress'.  fresh_db redirected
    # settings.dsn onto this test's database.
    async def _status() -> str | None:
        conn = await asyncpg.connect(dsn=settings.dsn)
        try:
            return await conn.fetchval(
                "SELECT discovery_status FROM cities WHERE id = $1", _test_brussels_city
            )
        finally:
            await conn.close()

    assert asyncio.run(_status()) == "pending"


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


def test_htmx_city_row_in_progress_no_trigger(
    client: TestClient, _test_city_in_progress: int
) -> None:
    """When city is 'in_progress', row polls but does NOT fire HX-Trigger.

    The in_progress row re-renders with hx-trigger=\"every 3s\" so the
    browser keeps polling.  It must NOT emit discoveryComplete because
    discovery hasn't finished yet — firing it early would refresh the
    websites table with stale data.
    """
    response = client.get(f"/htmx/country/BE/cities/{_test_city_in_progress}/row")
    assert response.status_code == 200
    assert "every 3s" in response.text
    assert "spinner-border" in response.text
    assert "HX-Trigger" not in response.headers


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
async def _app_pool(fresh_db: asyncpg.Connection) -> asyncpg.Pool:
    """Pool for the web app's query helpers, bound to the per-test database.

    ``fresh_db`` has already redirected ``get_pool()`` onto this test's
    private database, so the pool the app builds here is isolated.
    """
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

    # Score distribution — the database is pristine, so the buckets hold
    # exactly our four audited inserts.
    buckets = {b["bucket"]: b["cnt"] for b in stats["score_distribution"]}
    assert buckets["50+"] == 1  # 80
    assert buckets["20-49"] == 1  # 30
    assert buckets["0-19"] == 1  # 10
    assert buckets["negative"] == 1  # -5


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
    # Pristine database: no discovery_log rows exist yet.
    assert result == []


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
    # Pristine database: our single insert is the only activity row.
    assert len(result) == 1
    entry = result[0]
    assert entry["search_query"] == "test query sofia"
    assert entry["website_label"] == "Test Activity"
    assert entry["agent"] == "google_maps"
    assert entry["status"] == "found"
    assert entry["city_label"] == "Sofia"
    assert entry["website_url"] == "https://test-activity.example.com"


# ──────────────────────────────────────────────────────────────────────
# _run_city_discovery error handling (keep DiscoveryPipeline mock)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_city_discovery_marks_failed_on_error(fresh_db: asyncpg.Connection) -> None:
    """A failing background discovery marks the city 'failed' so polling stops.

    'failed' must be a status the DB CHECK constraint accepts (migration 004),
    otherwise this UPDATE would itself raise and leave the row stuck
    'in_progress'.  ``fresh_db`` has redirected ``get_pool()`` at this test's
    private database, so ``_run_city_discovery``'s internal pool connects here.
    """
    sofia_id = await fresh_db.fetchval(
        "SELECT id FROM cities WHERE country = 'BG' AND slug = 'sofia'"
    )
    assert sofia_id is not None, "Sofia must exist in the seeded database"

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
