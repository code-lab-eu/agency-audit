"""FastAPI + HTMX web dashboard for agency-audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from agency_audit.db import get_pool

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

        websites = await conn.fetch(
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

    return {
        "country": dict(country),
        "cities": [dict(r) for r in cities],
        "websites": [dict(r) for r in websites],
    }


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
