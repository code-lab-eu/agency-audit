# Agency Audit

Real Estate Radar — Website Discovery & Audit System.

Discovers, audits, and ranks real estate agency websites across 44 European countries for inclusion in the [Real Estate Radar](https://www.realestateradar.eu/) index.

## Quick Start (Docker Compose — full stack)

The easiest way to run the entire stack: PostgreSQL + agency-audit dashboard.

```bash
# Build and start both services (PostgreSQL + app)
docker compose up -d --build

# The dashboard is available at http://localhost:8000

# Initialize the database
docker compose exec app agency-audit db-init

# Seed 44 European countries
docker compose exec app agency-audit seed-countries

# Import cities from Geonames
docker compose exec app agency-audit import-geonames

# Check health
curl http://localhost:8000/health

# View stats
docker compose exec app agency-audit stats
```

## Quick Start (local development)

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

# Run the web frontend
uv run agency-audit serve
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

## Quality Assurance

Install the dev dependencies (ruff, pytest) into the project environment first:

```bash
uv sync --extra dev
```

The CI `quality` job runs the following checks. Run them locally before pushing —
they must all pass for the build to go green:

```bash
# Lint
uv run ruff check src/ tests/

# Format check (use `ruff format` without --check to auto-apply)
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/

# Tests
uv run pytest
```

To auto-fix most lint and all formatting issues:

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

## Database Schema

- **countries** — 44 European countries (ISO codes, names, active flag)
- **cities** — Cities from Geonames (population >= 50k, with lat/lng)
- **websites** — Agency websites with URL, label, score, audit_data (JSONB), status
- **website_cities** — Many-to-many junction (agencies ↔ cities)
- **discovery_log** — Audit trail for discovery runs

## Tech Stack

- Python 3.14+, asyncpg, FastAPI, Jinja2, httpx, selectolax, Playwright
- Typer + Rich CLI
- FastMCP for agent-DB bridge
- PostgreSQL 16+ (JSONB for flexible audit data)
- uv for package management, ruff for linting, pytest for testing
