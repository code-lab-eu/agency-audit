"""Tests for the FastAPI + HTMX web dashboard (web/app.py).

Tests all routes, HTMX partials, API endpoint, template helpers, and query helpers.
Uses FastAPI TestClient with mocked database pool (same pattern as test_loop.py).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agency_audit.web.app import _score_color, _status_badge, app

client = TestClient(app)


# ──────────────────────────────────────────────────────────────────────
# Template helpers
# ──────────────────────────────────────────────────────────────────────


def test_score_color_success():
    assert _score_color(50) == "text-success"
    assert _score_color(80) == "text-success"
    assert _score_color(100) == "text-success"


def test_score_color_warning():
    assert _score_color(20) == "text-warning"
    assert _score_color(49) == "text-warning"


def test_score_color_secondary():
    assert _score_color(0) == "text-secondary"
    assert _score_color(19) == "text-secondary"


def test_score_color_danger():
    assert _score_color(-10) == "text-danger"
    assert _score_color(-1) == "text-danger"


def test_status_badge_known():
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


def test_status_badge_unknown():
    result = str(_status_badge("unknown_status"))
    assert "bg-secondary" in result
    assert "Unknown Status" in result


# ──────────────────────────────────────────────────────────────────────
# Route: / (overview)
# ──────────────────────────────────────────────────────────────────────


def test_overview_route_templates_exist():
    """Sanity check: the overview page renders without crashing."""
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/")
        assert response.status_code == 200
        # Should contain basic HTML
        assert "<html" in response.text.lower() or "DOCTYPE" in response.text


# ──────────────────────────────────────────────────────────────────────
# Route: /countries
# ──────────────────────────────────────────────────────────────────────


def test_countries_route():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/countries")
        assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Route: /country/{iso}
# ──────────────────────────────────────────────────────────────────────


def test_country_detail_route_found():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value={"iso": "BG", "label": "Bulgaria"})
        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/country/BG")
        assert response.status_code == 200


def test_country_detail_route_not_found():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=None)

        response = client.get("/country/XX")
        assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /website/{website_id}
# ──────────────────────────────────────────────────────────────────────


def test_website_detail_route_found():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        import json

        audit_data = {"score": 75, "modules": {}}
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "url": "https://example.com",
                "label": "Test Agency",
                "score": 75,
                "audit_data": json.dumps(audit_data),
                "audit_status": "audited",
                "last_audited_at": None,
                "created_at": None,
                "maps_place_id": None,
                "address": "123 Main St",
                "phone": "+359123456",
            }
        )
        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/website/1")
        assert response.status_code == 200


def test_website_detail_route_not_found():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=None)

        response = client.get("/website/99999")
        assert response.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Route: /discovery
# ──────────────────────────────────────────────────────────────────────


def test_discovery_route():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "pending": 5,
                "in_progress": 2,
                "done": 10,
                "skipped": 1,
                "total": 18,
            }
        )

        response = client.get("/discovery")
        assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# HTMX partials
# ──────────────────────────────────────────────────────────────────────


def test_htmx_stats():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/htmx/stats")
        assert response.status_code == 200


def test_htmx_discovery_queue():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(
            return_value={"pending": 0, "in_progress": 0, "done": 0, "skipped": 0, "total": 0}
        )

        response = client.get("/htmx/discovery/queue")
        assert response.status_code == 200


def test_htmx_rediscover_city():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.execute = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(
            return_value={"pending": 1, "in_progress": 0, "done": 9, "skipped": 1, "total": 11}
        )

        response = client.post("/htmx/discovery/rediscover/42")
        assert response.status_code == 200
        # Verify the UPDATE was called
        mock_conn.execute.assert_any_call(
            "UPDATE cities SET discovery_status = 'pending' WHERE id = $1", 42
        )


def _city_row_record(status="pending"):
    return {
        "id": 42,
        "label": "Brussels",
        "slug": "brussels",
        "population": 1000000,
        "discovery_status": status,
        "website_count": 0,
        "audited_count": 0,
    }


def test_htmx_discover_city_triggers_background():
    with (
        patch("agency_audit.web.app.get_pool") as mock_get_pool,
        patch("agency_audit.web.app.settings") as mock_settings,
    ):
        mock_settings.google_maps_api_key = "test-key"
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=_city_row_record("pending"))

        # Prevent the background task from running real discovery.
        with patch("agency_audit.web.app._run_city_discovery", new=AsyncMock()):
            response = client.post("/htmx/country/BE/cities/42/discover")

        assert response.status_code == 200
        # City was flipped to in_progress and the row reflects it (with polling).
        mock_conn.execute.assert_any_call(
            "UPDATE cities SET discovery_status = 'in_progress' WHERE id = $1", 42
        )
        assert "every 3s" in response.text
        assert "spinner-border" in response.text


def test_htmx_discover_city_requires_api_key():
    with (
        patch("agency_audit.web.app.get_pool") as mock_get_pool,
        patch("agency_audit.web.app.settings") as mock_settings,
    ):
        mock_settings.google_maps_api_key = ""
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.execute = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=_city_row_record("pending"))

        response = client.post("/htmx/country/BE/cities/42/discover")
        assert response.status_code == 200
        assert "No Google Maps API key configured" in response.text
        # No status change when misconfigured.
        for call in mock_conn.execute.call_args_list:
            assert "in_progress" not in str(call)


def test_htmx_city_row():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=_city_row_record("done"))

        response = client.get("/htmx/country/BE/cities/42/row")
        assert response.status_code == 200
        # Done rows don't poll and show the refresh button.
        assert "every 3s" not in response.text
        assert "Refresh discovery" in response.text
        # Completion fires the event that refreshes the Websites table.
        assert response.headers.get("HX-Trigger") == "discoveryComplete"


def test_htmx_city_row_in_progress_no_trigger():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=_city_row_record("in_progress"))

        response = client.get("/htmx/country/BE/cities/42/row")
        assert response.status_code == 200
        # Still running: keep polling, don't refresh the Websites table yet.
        assert "every 3s" in response.text
        assert "HX-Trigger" not in response.headers


def test_htmx_country_websites():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "url": "https://example.be",
                    "label": "Example Agency",
                    "score": 42,
                    "audit_status": "audited",
                }
            ]
        )

        response = client.get("/htmx/country/BE/websites")
        assert response.status_code == 200
        assert 'id="websites-table"' in response.text
        assert "discoveryComplete from:body" in response.text
        assert "Example Agency" in response.text


def test_htmx_recent_activity():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/htmx/recent-activity")
        assert response.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# API endpoint
# ──────────────────────────────────────────────────────────────────────


def test_api_stats():
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchval = AsyncMock(return_value=0)
        mock_conn.fetch = AsyncMock(return_value=[])

        response = client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert "countries" in data
        assert "cities_total" in data
        assert "websites_total" in data
        assert "avg_score" in data


# ──────────────────────────────────────────────────────────────────────
# Query helpers (direct testing)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_stats():
    from agency_audit.web.app import _overview_stats

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchval = AsyncMock(return_value=10)
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"bucket": "50+", "cnt": 5},
                {"bucket": "20-49", "cnt": 3},
                {"bucket": "0-19", "cnt": 2},
            ]
        )

        stats = await _overview_stats(mock_pool)
        assert stats["countries"] == 10
        assert stats["cities_total"] == 10
        assert stats["websites_total"] == 10
        assert "score_distribution" in stats


@pytest.mark.asyncio
async def test_country_list():
    from agency_audit.web.app import _country_list

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "iso": "BG",
                    "label": "Bulgaria",
                    "city_count": 20,
                    "cities_done": 5,
                    "cities_pending": 15,
                    "websites_discovered": 30,
                    "websites_audited": 10,
                    "avg_score": "75.50",
                }
            ]
        )

        result = await _country_list(mock_pool)
        assert len(result) == 1
        assert result[0]["iso"] == "BG"


@pytest.mark.asyncio
async def test_country_detail():
    from agency_audit.web.app import _country_detail

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value={"iso": "BG", "label": "Bulgaria"})
        mock_conn.fetch = AsyncMock(return_value=[])

        result = await _country_detail(mock_pool, "BG")
        assert result is not None
        assert result["country"]["iso"] == "BG"
        assert "cities" in result
        assert "websites" in result


@pytest.mark.asyncio
async def test_country_detail_none():
    from agency_audit.web.app import _country_detail

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=None)

        result = await _country_detail(mock_pool, "XX")
        assert result is None


@pytest.mark.asyncio
async def test_website_detail():
    from agency_audit.web.app import _website_detail

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "url": "https://example.com",
                "label": "Test",
                "score": 80,
                "audit_data": '{"score":80}',
                "audit_status": "audited",
                "last_audited_at": None,
                "created_at": None,
                "maps_place_id": None,
                "address": None,
                "phone": None,
            }
        )
        mock_conn.fetch = AsyncMock(return_value=[])

        result = await _website_detail(mock_pool, 1)
        assert result is not None
        assert result["website"]["url"] == "https://example.com"
        assert "cities" in result
        assert "discovery_logs" in result


@pytest.mark.asyncio
async def test_website_detail_none():
    from agency_audit.web.app import _website_detail

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchrow = AsyncMock(return_value=None)

        result = await _website_detail(mock_pool, 999)
        assert result is None


@pytest.mark.asyncio
async def test_discovery_queue():
    from agency_audit.web.app import _discovery_queue

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "pending": 3,
                "in_progress": 1,
                "done": 5,
                "skipped": 0,
                "total": 9,
            }
        )

        result = await _discovery_queue(mock_pool)
        assert result["pending"] == []
        assert result["counts"]["pending"] == 3
        assert result["counts"]["total"] == 9


@pytest.mark.asyncio
async def test_recent_activity():
    from agency_audit.web.app import _recent_activity

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(return_value=[])

        result = await _recent_activity(mock_pool)
        assert result == []


@pytest.mark.asyncio
async def test_recent_activity_with_data():
    from agency_audit.web.app import _recent_activity

    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "city_id": 10,
                    "website_id": 100,
                    "agent": "google_maps",
                    "search_query": "test",
                    "status": "found",
                    "created_at": None,
                    "city_label": "Sofia",
                    "website_label": "Test Agency",
                    "website_url": "https://example.com",
                }
            ]
        )

        result = await _recent_activity(mock_pool, limit=5)
        assert len(result) == 1
        assert result[0]["agent"] == "google_maps"


# ──────────────────────────────────────────────────────────────────────
# Health endpoint
# ──────────────────────────────────────────────────────────────────────


def test_health_healthy():
    """Health endpoint returns 200 when database is reachable."""
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_pool = MagicMock()
        mock_get_pool.return_value = mock_pool

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value = mock_ctx

        mock_conn.fetchval = AsyncMock(return_value=1)

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["db"] == "connected"


def test_health_unhealthy():
    """Health endpoint returns 503 when database is unreachable."""
    with patch("agency_audit.web.app.get_pool") as mock_get_pool:
        mock_get_pool.side_effect = RuntimeError("connection refused")

        response = client.get("/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["db"] == "disconnected"
        assert "connection refused" in data["detail"]
