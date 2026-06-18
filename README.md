# Agency Audit

Real Estate Radar — Website Discovery & Audit System.

Discovers, audits, and ranks real estate agency websites across 44 European countries for inclusion in the [Real Estate Radar](https://www.realestateradar.eu/) index.

## Quick Start

```bash
# Install dependencies
uv sync

# Start PostgreSQL (requires Docker)
docker compose up -d

# Or use a local PostgreSQL — configure via .env
cp .env.example .env

# Apply migrations
uv run agency-audit db-init

# Seed 44 countries
uv run agency-audit seed-countries

# Import cities from Geonames
uv run agency-audit import-geonames

# View stats
uv run agency-audit stats
```

## Project Structure

```
src/agency_audit/
  __init__.py
  cli.py          # Typer CLI
  config.py       # pydantic-settings
  db.py           # asyncpg connection pool
  geonames.py     # Geonames city import utility
  migrations/     # SQL migration files
  seed/           # Seed data (countries)
  web/            # FastAPI + Jinja2 dashboard
  audit/          # Website audit logic
  tests/          # Test suite
```

## Database Schema

- **countries** — 44 European countries (ISO codes, names, active flag)
- **cities** — Cities from Geonames (population >= 50k, with lat/lng)
- **websites** — Agency websites with URL, label, score, audit_data (JSONB), status
- **website_cities** — Many-to-many junction (agencies ↔ cities)
- **discovery_log** — Audit trail for discovery runs

## Tech Stack

- Python 3.12+, asyncpg, FastAPI, Jinja2, httpx, selectolax, Playwright
- Typer + Rich CLI
- FastMCP for agent-DB bridge
- PostgreSQL 16+ (JSONB for flexible audit data)
- uv for package management, ruff for linting, pytest for testing
