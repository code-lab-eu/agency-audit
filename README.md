# Agency Audit

Real Estate Radar — Website Discovery & Audit System.

Discovers, audits, and ranks real estate agency websites across 44 European
countries for inclusion in the [Real Estate Radar](https://www.realestateradar.eu/) index.

## Quick Start

```bash
# Install dependencies
uv sync

# Start PostgreSQL (requires Docker)
docker compose up -d

# Or use a local PostgreSQL — copy and edit .env first
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

## Environment Variables

All configuration is read from environment variables prefixed with
`AGENCY_AUDIT_`.  Copy `.env.example` to `.env` and fill in your values:

| Variable | Default | Description |
|---|---|---|
| `AGENCY_AUDIT_PG_HOST` | `localhost` | PostgreSQL host |
| `AGENCY_AUDIT_PG_PORT` | `5432` | PostgreSQL port |
| `AGENCY_AUDIT_PG_USER` | `agency_audit` | PostgreSQL user |
| `AGENCY_AUDIT_PG_PASSWORD` | *(empty)* | PostgreSQL password |
| `AGENCY_AUDIT_PG_DATABASE` | `agency_audit` | PostgreSQL database |
| `AGENCY_AUDIT_GEONAMES_MIN_POPULATION` | `50000` | Min population for city import |
| `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` | *(empty)* | Google Maps Places API key (required for `discover`) |

### Google Maps API Key

The `discover` command requires a Google Maps Places API key.  Without one,
discovery exits with an error.  Set it in `.env`:

```bash
AGENCY_AUDIT_GOOGLE_MAPS_API_KEY=your-key-here
```

## Operational Commands

### Discover agencies

Finds real estate agencies via the Google Maps Places API.
**Requires `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY`.**

```bash
# Discover agencies in Bulgaria (limit to 5 cities)
uv run agency-audit discover --country BG --max-cities 5

# Discover in multiple countries
uv run agency-audit discover --countries "BG,RO,GR" --max-cities 3
```

### Audit websites

Run the full 7-module audit pipeline on a URL or database record.

```bash
# Audit a URL directly (table output)
uv run agency-audit audit --url "https://example-agency.com"

# JSON output
uv run agency-audit audit --url "https://example-agency.com" --output json

# Audit a website already in the database and store results
uv run agency-audit audit --website-id 42 --output db

# Batch audit multiple URLs concurrently
uv run agency-audit batch-audit --urls "https://a.bg,https://b.bg" --concurrency 3
```

### Web dashboard

Start the FastAPI + HTMX dashboard.

```bash
# Default: http://0.0.0.0:8000
uv run agency-audit serve

# Custom host, port, and auto-reload
uv run agency-audit serve --host 127.0.0.1 --port 8080 --reload
```

### Full country loop

Execute the complete operational pipeline for one country:
**discover → audit → QC → re-audit**.

```bash
uv run agency-audit run --country BG
```

```bash
# Custom concurrency and city limit
uv run agency-audit run --country BG --max-cities 10 --concurrency 5

# Skip phases
uv run agency-audit run --country BG --skip-discovery --skip-qc
```

### Run all countries

Execute the full loop for every active country sequentially.

```bash
uv run agency-audit run-all

# Limit to specific countries
uv run agency-audit run-all --countries "BG,RO,GR"
```

### Supporting commands

```bash
# Database statistics
uv run agency-audit stats

# Quality control checks
uv run agency-audit qc --action run
uv run agency-audit qc --action list-review

# Re-audit queue management
uv run agency-audit reaudit --action queue --country BG
uv run agency-audit reaudit --action trigger --limit 50

# Pipeline progress
uv run agency-audit progress
```

## Test Data

Use `src/agency_audit/scripts/seed_test_data.py` to populate the database
with sample agencies and audit records for local development and dashboard
testing:

```bash
uv run python src/agency_audit/scripts/seed_test_data.py
```

This inserts 5 sample websites (3 audited with scores, 1 blocked, 1 pending)
and links them to Bulgarian cities.  Requires a running PostgreSQL instance
with a migrated and seeded database.

For CI, the equivalent setup is handled by `.github/scripts/ci-db-setup.py`.

## Project Structure

```
src/agency_audit/
  __init__.py
  cli.py          # Typer CLI
  config.py       # pydantic-settings
  db.py           # asyncpg connection pool
  discovery.py    # Google Maps Places discovery pipeline
  geonames.py     # Geonames city import utility
  mcp_server.py   # FastMCP agent-DB bridge
  migrations/     # SQL migration files
  seed/           # Seed data (countries)
  scripts/        # Helper scripts (seed_test_data, discovery_helper)
  web/            # FastAPI + Jinja2 dashboard
  audit/          # Website audit logic
  loop/           # Operational loop (discover → audit → QC → reaudit)
  tests/          # Test suite
```

## Quality Assurance

Install the dev dependencies (ruff, pytest, mypy) into the project environment
first:

```bash
uv sync --extra dev
```

The CI `quality` job runs the following checks. Run them locally before
pushing — they must all pass for the build to go green.

**Quick gate:** `scripts/qa.sh` runs lint, format check, mypy, and tests in one
pass.  Use `scripts/qa.sh --fix` to auto-apply fixes first.

```bash
# All-in-one (mirrors CI)
scripts/qa.sh

# Auto-fix + re-check
scripts/qa.sh --fix
```

Or run each check individually:

```bash
# Lint
uv run ruff check src/ tests/

# Format check (use `ruff format` without --check to auto-apply)
uv run ruff format --check src/ tests/

# Type check
uv run mypy src/

# Test suite (no live database required)
uv run --extra dev pytest
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

- Python 3.12+, asyncpg, FastAPI, Jinja2, httpx, selectolax, Playwright
- Typer + Rich CLI
- FastMCP for agent-DB bridge
- PostgreSQL 16+ (JSONB for flexible audit data)
- uv for package management, ruff for linting, pytest for testing
