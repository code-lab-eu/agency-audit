"""FastAPI + HTMX web dashboard for agency-audit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from agency_audit.config import settings
from agency_audit.db import get_pool

logger = logging.getLogger(__name__)

# --- App setup -----------------------------------------------------------------

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Agency Audit Dashboard", docs_url="/api/docs", redoc_url=None)


# --- Template helpers ----------------------------------------------------------


def _score_color(score: int) -> str:
    """Map a score to a CSS color class."""
    if score >= 50:
        return "text-success"
    if score >= 20:
        return "text-warning"
    if score >= 0:
        return "text-secondary"
    return "text-danger"


templates.env.filters["score_color"] = _score_color


def _status_badge(status: str) -> str:
    """Map a status string to a Bootstrap badge class."""
    mapping = {
        "pending": "bg-secondary",
        "in_progress": "bg-info",
        "auditing": "bg-info",
        "audited": "bg-success",
        "done": "bg-success",
        "failed": "bg-danger",
        "skipped": "bg-warning",
        "found": "bg-primary",
        "searched": "bg-secondary",
    }
    css = mapping.get(status, "bg-secondary")
    label = status.replace("_", " ").title()
    return Markup(f'<span class="badge {css}">{label}</span>')


templates.env.filters["status_badge"] = _status_badge


# --- Query helpers -------------------------------------------------------------


async def _overview_stats(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        countries = await conn.fetchval("SELECT COUNT(*) FROM countries WHERE active")
        cities_total = await conn.fetchval("SELECT COUNT(*) FROM cities")
        cities_done = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'done'"
        )
        cities_in_progress = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'in_progress'"
        )
        cities_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE discovery_status = 'pending'"
        )
        websites_total = await conn.fetchval("SELECT COUNT(*) FROM websites")
        websites_audited = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'audited'"
        )
        websites_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM websites WHERE audit_status = 'pending'"
        )
        avg_score = await conn.fetchval(
            "SELECT COALESCE(AVG(score), 0)::numeric(10,2) "
            "FROM websites WHERE audit_status = 'audited'"
        )
        # Score distribution buckets
        dist = await conn.fetch(
            """
            SELECT
                CASE
                    WHEN score >= 50 THEN '50+'
                    WHEN score >= 20 THEN '20-49'
                    WHEN score >= 0  THEN '0-19'
                    ELSE 'negative'
                END AS bucket,
                COUNT(*) AS cnt
            FROM websites
            WHERE audit_status = 'audited'
            GROUP BY bucket
            ORDER BY bucket
            """
        )

    return {
        "countries": countries,
        "cities_total": cities_total,
        "cities_done": cities_done,
        "cities_in_progress": cities_in_progress,
        "cities_pending": cities_pending,
        "websites_total": websites_total,
        "websites_audited": websites_audited,
        "websites_pending": websites_pending,
        "avg_score": float(avg_score),
        "score_distribution": [dict(r) for r in dist],
    }


async def _country_list(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.iso,
                c.label,
                COUNT(DISTINCT ci.id) AS city_count,
                COUNT(DISTINCT CASE WHEN ci.discovery_status = 'done'
                    THEN ci.id END) AS cities_done,
                COUNT(DISTINCT CASE WHEN ci.discovery_status = 'pending'
                    THEN ci.id END) AS cities_pending,
                COUNT(DISTINCT w.id) AS websites_discovered,
                COUNT(DISTINCT CASE WHEN w.audit_status = 'audited'
                    THEN w.id END) AS websites_audited,
                COALESCE(AVG(CASE WHEN w.audit_status = 'audited'
                    THEN w.score END), 0)::numeric(10,2) AS avg_score
            FROM countries c
            LEFT JOIN cities ci ON ci.country = c.iso
            LEFT JOIN website_cities wc ON wc.city_id = ci.id
            LEFT JOIN websites w ON w.id = wc.website_id
            WHERE c.active = true
            GROUP BY c.iso, c.label
            ORDER BY c.label
            """
        )
    return [dict(r) for r in rows]


async def _country_websites(pool: asyncpg.Pool, iso: str) -> list[dict[str, Any]]:
    """Websites discovered for a country, highest score first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT w.id, w.url, w.label, w.score, w.audit_status
            FROM websites w
            JOIN website_cities wc ON wc.website_id = w.id
            JOIN cities ci ON ci.id = wc.city_id
            WHERE ci.country = $1
            ORDER BY w.score DESC, w.label
            """,
            iso.upper(),
        )
    return [dict(r) for r in rows]


async def _country_detail(pool: asyncpg.Pool, iso: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        country = await conn.fetchrow(
            "SELECT iso, label FROM countries WHERE iso = $1", iso.upper()
        )
        if country is None:
            return None

        cities = await conn.fetch(
            """
            SELECT
                ci.id, ci.label, ci.slug, ci.population, ci.discovery_status,
                COUNT(DISTINCT wc.website_id) AS website_count,
                COUNT(DISTINCT CASE WHEN w.audit_status = 'audited'
                    THEN wc.website_id END) AS audited_count
            FROM cities ci
            LEFT JOIN website_cities wc ON wc.city_id = ci.id
            LEFT JOIN websites w ON w.id = wc.website_id
            WHERE ci.country = $1
            GROUP BY ci.id, ci.label, ci.slug, ci.population, ci.discovery_status
            ORDER BY ci.population DESC
            """,
            iso.upper(),
        )

    websites = await _country_websites(pool, iso)

    return {
        "country": dict(country),
        "cities": [dict(r) for r in cities],
        "websites": websites,
    }


async def _city_row_data(pool: asyncpg.Pool, city_id: int, iso: str) -> dict[str, Any] | None:
    """Fetch the single-city fields the cities table row needs (for HTMX re-render).

    Bound to ``iso`` so a city is only ever looked up in the country whose page
    it is rendered on — a request for a city that belongs to another country
    returns ``None`` (404) rather than rendering on the wrong page.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                ci.id, ci.label, ci.slug, ci.population, ci.discovery_status,
                COUNT(DISTINCT wc.website_id) AS website_count,
                COUNT(DISTINCT CASE WHEN w.audit_status = 'audited'
                    THEN wc.website_id END) AS audited_count
            FROM cities ci
            LEFT JOIN website_cities wc ON wc.city_id = ci.id
            LEFT JOIN websites w ON w.id = wc.website_id
            WHERE ci.id = $1 AND ci.country = $2
            GROUP BY ci.id, ci.label, ci.slug, ci.population, ci.discovery_status
            """,
            city_id,
            iso.upper(),
        )
    return dict(row) if row else None


async def _run_city_discovery(city_id: int, iso: str) -> None:
    """Background task: run Google Maps discovery for a single city.

    ``DiscoveryPipeline.discover_city`` flips ``discovery_status`` to ``done``
    when it finishes; on failure we mark the city ``failed`` so the polling row
    stops spinning. The city's own stored ``country`` is used for the discovery
    query templates — never the caller-supplied ISO — and the lookup is bound to
    that country so a mismatched request finds nothing.
    """
    from agency_audit.discovery import DiscoveryPipeline

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, label, slug, country, latitude, longitude "
            "FROM cities WHERE id = $1 AND country = $2",
            city_id,
            iso.upper(),
        )
    if row is None:
        return

    pipeline = DiscoveryPipeline()
    try:
        await pipeline.discover_city(
            city_id=row["id"],
            city_label=row["label"],
            city_slug=row["slug"],
            country_iso=row["country"],
            latitude=row["latitude"],
            longitude=row["longitude"],
        )
    except Exception:
        logger.exception("Background discovery failed for city %s", city_id)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE cities SET discovery_status = 'failed' WHERE id = $1", city_id
            )
    finally:
        await pipeline.close()


async def _website_detail(pool: asyncpg.Pool, website_id: int) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, url, label, score, audit_data, audit_status,
                   last_audited_at, created_at, maps_place_id, address, phone
            FROM websites WHERE id = $1
            """,
            website_id,
        )
        if row is None:
            return None

        cities = await conn.fetch(
            """
            SELECT c.id, c.label, c.slug, c.country, wc.discovered_via
            FROM website_cities wc
            JOIN cities c ON c.id = wc.city_id
            WHERE wc.website_id = $1
            """,
            website_id,
        )

        discovery_logs = await conn.fetch(
            """
            SELECT id, agent, search_query, status, created_at
            FROM discovery_log
            WHERE website_id = $1
            ORDER BY created_at DESC
            """,
            website_id,
        )

    import json

    audit_data = row["audit_data"]
    if isinstance(audit_data, str):
        audit_data = json.loads(audit_data)

    return {
        "website": {**dict(row), "audit_data": audit_data},
        "cities": [dict(c) for c in cities],
        "discovery_logs": [dict(d) for d in discovery_logs],
    }


async def _discovery_queue(pool: asyncpg.Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT ci.id, ci.label, ci.slug, ci.country, ci.population,
                   co.label AS country_label
            FROM cities ci
            JOIN countries co ON co.iso = ci.country
            WHERE ci.discovery_status = 'pending'
            ORDER BY ci.population DESC
            LIMIT 50
            """
        )
        in_progress = await conn.fetch(
            """
            SELECT ci.id, ci.label, ci.slug, ci.country, ci.population,
                   co.label AS country_label
            FROM cities ci
            JOIN countries co ON co.iso = ci.country
            WHERE ci.discovery_status = 'in_progress'
            ORDER BY ci.population DESC
            """
        )
        done = await conn.fetch(
            """
            SELECT ci.id, ci.label, ci.slug, ci.country, ci.population,
                   co.label AS country_label,
                   COUNT(DISTINCT wc.website_id) AS websites_found
            FROM cities ci
            JOIN countries co ON co.iso = ci.country
            LEFT JOIN website_cities wc ON wc.city_id = ci.id
            WHERE ci.discovery_status = 'done'
            GROUP BY ci.id, co.label
            ORDER BY ci.population DESC
            LIMIT 50
            """
        )
        counts = await conn.fetchrow(
            """
            SELECT
                SUM(CASE WHEN discovery_status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN discovery_status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN discovery_status = 'done' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN discovery_status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                COUNT(*) AS total
            FROM cities
            """
        )

    return {
        "pending": [dict(r) for r in pending],
        "in_progress": [dict(r) for r in in_progress],
        "done": [dict(r) for r in done],
        "counts": dict(counts) if counts else {},
    }


async def _recent_activity(pool: asyncpg.Pool, limit: int = 20) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT dl.id, dl.city_id, dl.website_id, dl.agent, dl.search_query,
                   dl.status, dl.created_at,
                   ci.label AS city_label,
                   w.label AS website_label, w.url AS website_url
            FROM discovery_log dl
            LEFT JOIN cities ci ON ci.id = dl.city_id
            LEFT JOIN websites w ON w.id = dl.website_id
            ORDER BY dl.created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


# --- Routes --------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    pool = await get_pool()
    stats = await _overview_stats(pool)
    activity = await _recent_activity(pool, 15)
    return templates.TemplateResponse(
        request,
        "overview.html",
        {"stats": stats, "activity": activity, "page": "overview"},
    )


@app.get("/countries", response_class=HTMLResponse)
async def countries(request: Request):
    pool = await get_pool()
    countries_data = await _country_list(pool)
    return templates.TemplateResponse(
        request,
        "countries.html",
        {"countries": countries_data, "page": "countries"},
    )


@app.get("/country/{iso}", response_class=HTMLResponse)
async def country_detail(request: Request, iso: str):
    pool = await get_pool()
    data = await _country_detail(pool, iso)
    if data is None:
        return HTMLResponse("<h1>Country not found</h1>", status_code=404)
    return templates.TemplateResponse(
        request,
        "country_detail.html",
        {"data": data, "page": "countries"},
    )


@app.get("/website/{website_id}", response_class=HTMLResponse)
async def website_detail(request: Request, website_id: int):
    pool = await get_pool()
    data = await _website_detail(pool, website_id)
    if data is None:
        return HTMLResponse("<h1>Website not found</h1>", status_code=404)
    return templates.TemplateResponse(
        request,
        "website_detail.html",
        {"data": data, "page": "websites"},
    )


@app.get("/discovery", response_class=HTMLResponse)
async def discovery_queue(request: Request):
    pool = await get_pool()
    data = await _discovery_queue(pool)
    return templates.TemplateResponse(
        request,
        "discovery.html",
        {"data": data, "page": "discovery"},
    )


# --- HTMX partials -------------------------------------------------------------


@app.get("/htmx/stats", response_class=HTMLResponse)
async def htmx_stats(request: Request):
    """Partial: overview stats card for HTMX refresh."""
    pool = await get_pool()
    stats = await _overview_stats(pool)
    return templates.TemplateResponse(
        request,
        "_stats.html",
        {"stats": stats},
    )


@app.get("/htmx/discovery/queue", response_class=HTMLResponse)
async def htmx_discovery_queue(request: Request):
    """Partial: discovery queue table for HTMX refresh."""
    pool = await get_pool()
    data = await _discovery_queue(pool)
    return templates.TemplateResponse(
        request,
        "_discovery_queue.html",
        {"data": data},
    )


@app.post("/htmx/discovery/rediscover/{city_id}", response_class=HTMLResponse)
async def htmx_rediscover_city(request: Request, city_id: int):
    """Reset a city's discovery_status to 'pending' for re-discovery."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE cities SET discovery_status = 'pending' WHERE id = $1", city_id)
    data = await _discovery_queue(pool)
    return templates.TemplateResponse(
        request,
        "_discovery_queue.html",
        {"data": data},
    )


@app.post(
    "/htmx/country/{iso}/cities/{city_id}/discover",
    response_class=HTMLResponse,
)
async def htmx_discover_city(
    request: Request,
    iso: str,
    city_id: int,
    background_tasks: BackgroundTasks,
):
    """Trigger discovery for a single city in the background and return its row."""
    pool = await get_pool()
    city = await _city_row_data(pool, city_id, iso)
    if city is None:
        return HTMLResponse("Not found", status_code=404)

    # Guard: a Maps API key is required, else discovery would mark the city
    # 'done' with zero results. Surface the misconfiguration instead.
    if not settings.google_maps_api_key:
        return templates.TemplateResponse(
            request,
            "_city_row.html",
            {"ci": city, "iso": iso, "error": "No Google Maps API key configured"},
        )

    # Atomic, idempotent transition: only the request that actually flips the row
    # out of 'in_progress' enqueues a discovery job. Concurrent double-clicks see
    # no rows updated and fall through to re-rendering the already-running row,
    # so we never enqueue duplicate Google Places runs for the same city.
    async with pool.acquire() as conn:
        transitioned = await conn.fetchval(
            "UPDATE cities SET discovery_status = 'in_progress' "
            "WHERE id = $1 AND country = $2 AND discovery_status <> 'in_progress' "
            "RETURNING id",
            city_id,
            iso.upper(),
        )
    if transitioned is not None:
        background_tasks.add_task(_run_city_discovery, city_id, iso)

    city["discovery_status"] = "in_progress"
    return templates.TemplateResponse(
        request,
        "_city_row.html",
        {"ci": city, "iso": iso},
    )


@app.get(
    "/htmx/country/{iso}/cities/{city_id}/row",
    response_class=HTMLResponse,
)
async def htmx_city_row(request: Request, iso: str, city_id: int):
    """Re-render a single city row. In-progress rows poll this to update status.

    When the city is no longer ``in_progress`` (discovery finished), emit an
    ``HX-Trigger: discoveryComplete`` header so the Websites table refreshes to
    show the newly discovered agencies. Polling stops on the same response.
    """
    pool = await get_pool()
    city = await _city_row_data(pool, city_id, iso)
    if city is None:
        return HTMLResponse("Not found", status_code=404)
    response = templates.TemplateResponse(
        request,
        "_city_row.html",
        {"ci": city, "iso": iso},
    )
    if city["discovery_status"] != "in_progress":
        response.headers["HX-Trigger"] = "discoveryComplete"
    return response


@app.get("/htmx/country/{iso}/websites", response_class=HTMLResponse)
async def htmx_country_websites(request: Request, iso: str):
    """Partial: the country's Websites table, refreshed when discovery completes."""
    pool = await get_pool()
    websites = await _country_websites(pool, iso)
    return templates.TemplateResponse(
        request,
        "_websites_table.html",
        {"websites": websites, "iso": iso},
    )


@app.get("/htmx/recent-activity", response_class=HTMLResponse)
async def htmx_recent_activity(request: Request):
    """Partial: recent activity table for HTMX refresh."""
    pool = await get_pool()
    activity = await _recent_activity(pool, 15)
    return templates.TemplateResponse(
        request,
        "_recent_activity.html",
        {"activity": activity},
    )


# --- API endpoints -------------------------------------------------------------


@app.get("/api/stats")
async def api_stats():
    """JSON API for stats."""
    pool = await get_pool()
    return JSONResponse(await _overview_stats(pool))


@app.get("/health")
async def health():
    """Health check endpoint.

    Returns 200 with ``{"status": "healthy", "db": "connected"}`` when the
    service is ready and the database is reachable.  Returns 503 if the
    database cannot be reached, so orchestrators (Docker, K8s, load-balancers)
    can take the instance out of rotation.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        return JSONResponse(
            {"status": "unhealthy", "db": "disconnected", "detail": str(exc)},
            status_code=503,
        )
    return JSONResponse({"status": "healthy", "db": "connected"})
