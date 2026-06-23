# AGENTS.md — Agency Audit

Instructions for AI coding agents and human contributors working on this
codebase. Read this before writing any code.

## Project overview

**Real Estate Radar — Website Discovery & Audit System.** Discovers, audits,
and ranks real estate agency websites across 44 European countries for the
[Real Estate Radar](https://www.realestateradar.eu/) index.

Core flow: discover agencies via Google Maps (API or browser scrape) →
audit each site with 7 checks → score 0–100 → queue re-audits for stale
entries.  The full loop (`discover → audit → QC → reaudit`) runs
country-by-country through the orchestrator.

## Prerequisites

- Python **3.12+** (CI gate enforces ≥3.12; uv lock targets 3.12)
- [uv](https://docs.astral.sh/uv/) for package management (no pip required)
- **PostgreSQL 16+** with the `agency_audit` database
- Docker (optional — `docker compose up -d` spins up a disposable PG instance)

## Setup

```bash
git clone git@github.com:code-lab-eu/agency-audit.git
cd agency-audit

# Install dependencies (creates .venv automatically)
uv sync --extra dev

# PostgreSQL — pick one:
#   A) Docker (quickest)
docker compose up -d
#   B) Local Postgres — copy the example env file and fill in credentials
cp .env.example .env

# Initialise the database
uv run agency-audit db-init

# Seed 44 European countries
uv run agency-audit seed-countries

# Import city data from Geonames (~50k+ cities with population ≥50k)
uv run agency-audit import-geonames

# Verify everything is wired up
uv run agency-audit stats
```

Environment variables (all prefixed `AGENCY_AUDIT_`; see
`src/agency_audit/config.py`):

| Variable                       | Default         |
|--------------------------------|-----------------|
| `AGENCY_AUDIT_PG_HOST`         | `localhost`     |
| `AGENCY_AUDIT_PG_PORT`         | `5432`          |
| `AGENCY_AUDIT_PG_USER`         | `agency_audit`  |
| `AGENCY_AUDIT_PG_PASSWORD`     | *(empty)*       |
| `AGENCY_AUDIT_PG_DATABASE`     | `agency_audit`  |
| `AGENCY_AUDIT_GEONAMES_MIN_POPULATION` | `50000`   |

## Common commands

```bash
# Run the test suite (NOT dependent on a live database — pool is mocked)
uv run --extra dev pytest

# Run with coverage
uv run --extra dev pytest --cov=src/agency_audit --cov-report=term

# Lint
uvx ruff check src/ tests/

# Format check (CI gate, enforce double-quotes)
uvx ruff format --check src/ tests/

# Type check (CI gate)
uvx --from mypy mypy src/

# Apply auto-fixes before committing
uvx ruff check --fix src/ tests/
uvx ruff format src/ tests/

# Start the web dashboard (FastAPI + HTMX, http://0.0.0.0:8000)
uv run agency-audit serve

# Audit a single URL with the full 7-module pipeline
uv run agency-audit audit --url "https://example-agency.com"

# Batch audit (comma-separated URLs, concurrent)
uv run agency-audit batch-audit --urls "https://a.bg,https://b.bg" --concurrency 3

# Run the full operational loop for one country
uv run agency-audit run --country BG

# Discovery only (Google Maps API + browser fallback)
uv run agency-audit discover --country BG --max-cities 5

# Start the MCP server (agent-DB bridge)
uv run agency-audit-mcp
```

## Project layout

```
agency-audit/
  src/agency_audit/
    __init__.py               # version 0.1.0
    cli.py                    # Typer CLI — all commands (db, seed, audit, discover, run, serve, stats)
    config.py                 # pydantic-settings (AGENCY_AUDIT_* env vars)
    db.py                     # asyncpg pool (get_pool / close_pool)
    discovery.py              # Google Maps Places discovery pipeline
    geonames.py               # Cities15000.zip import from Geonames
    mcp_server.py             # FastMCP tools (get_next_city, submit_audit, …)
    scoring_config.yaml       # Weights for the 0-100 audit score
    audit/                    # Website audit logic (7 modules)
      __init__.py             # Public API: audit_website, audit_websites, AuditData, compute_score
      models.py               # Dataclasses: RobotsResult, AuditData, TechStackResult, …
      robots.py               # robots.txt fetch & parse
      anti_scraping.py        # Cloudflare/reCAPTCHA/JS-only detection
      api_detection.py        # GraphQL/REST/JSON-LD detection
      property_count.py       # Listing count estimation (listing page, sitemap, API)
      listing_quality.py      # Structured data, images, prices, locations
      tech_stack.py           # Framework/CDN/hosting identification
      scoring.py              # Configurable scoring formula
      auditor.py              # Main orchestrator combining all checks
      playwright_fetch.py     # On-demand JS rendering for SPAs
    loop/                     # Operational loop (discover → audit → QC → reaudit)
      __init__.py             # Public API: run_country, run_qc_checks, schedule_reaudits, …
      orchestrator.py         # Country-by-country loop executor
      qc.py                   # Duplicate detection, suspicious score flagging
      reaudit.py              # Re-audit scheduling (stale >30 days)
      retry.py                # Exponential backoff retry (3 attempts)
      tracking.py             # Progress tracking via discovery_log / audit_log
    migrations/               # SQL migrations (001_init, 002_discovery_status, 003_audit_log)
    seed/                     # Seed data (countries.sql)
    web/                      # FastAPI + HTMX + Jinja2 dashboard
      app.py                  # FastAPI app, routes, template filters
      templates/              # Jinja2 templates (overview, countries, discovery, …)
  tests/
    test_basics.py            # Sanity checks (imports, version)
    test_audit.py             # Unit tests for audit modules
    test_loop.py              # Unit tests for loop (QC, retry, tracking, orchestrator)
    test_mcp_server.py        # MCP server integration tests (needs live DB)
    test_audit_coverage.py    # Comprehensive audit coverage tests
    integration/test_pipeline.py  # End-to-end pipeline tests
  pyproject.toml              # Project metadata, deps, ruff, pytest, mypy config
  docker-compose.yml          # Local PostgreSQL 16
  scoring_config.yaml         # Audit scoring weights
  .github/workflows/agency-audit-ci.yml  # CI: lint, format check, mypy
```

## Code style & conventions

- **Line length:** 100 characters (`pyproject.toml` → `tool.ruff.line-length`)
- **Quote style:** double quotes (enforced by ruff format)
- **Lint rules:** `E, F, I, UP, B, SIM` (pyflakes, import sorting, pyupgrade,
  flake8-bugbear, code simplification)
- **Imports:** `from __future__ import annotations` at the top of every
  type-annotated module (targets Python ≥3.12 but keeps compatibility)
  Import order: stdlib → third-party → local (enforced by `I` rule)
- **Types:** Pydantic `BaseSettings` for config, dataclasses for audit result
  models, type hints on all public functions
- **Async:** All database operations and HTTP calls are `async`. The connection
  pool lives at module level in `db.py` (`get_pool()` / `close_pool()`)
- **CLI:** Typer app with Rich tables and `asyncio.run()` wrappers for async
  commands
- **Error handling:** Retry with exponential backoff (`loop/retry.py`), explicit
  `finally: await close_pool()` blocks, logger (not print) in library code

## Testing

- **Test runner:** pytest with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`)
- **Mocking database:** Patch `agency_audit.*.get_pool` with an async mock
  (see `tests/test_loop.py` for the pattern).  The default test run must NEVER
  require a live PostgreSQL instance
- **Test structure:** Module-level test files mirror source layout
  (`test_audit.py` → `src/agency_audit/audit/`, `test_loop.py` →
  `src/agency_audit/loop/`)
- **Before pushing:** `uv run --extra dev pytest` must be green and
  `uvx ruff check .` must be clean

## Contribution workflow

1. Create a feature branch off `master`:
   `git checkout master && git pull && git checkout -b feat/<slug>` or
   `git checkout -b fix/<slug>`
2. Make changes, write tests, run the full suite
3. Commit with [conventional commit](https://www.conventionalcommits.org/)
   prefixes (`feat:`, `fix:`, `test:`, `refactor:`)
4. Push your branch and open a PR against `master` — CI runs ruff lint,
   ruff format check, and mypy.  A human reviews before merge
5. Never push directly to `master`

## Additional context

- **Scoring:** Weights live in `scoring_config.yaml` (loaded at runtime).
  Adjust weights there — no code changes needed for tuning
- **MCP server:** Exposes 5 tools (`get_next_city`, `report_website`,
  `get_unaudited_website`, `submit_audit`, `get_stats`) for agent-driven
  operation.  Run with `uv run agency-audit-mcp`
- **Discovery fallback:** If `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` (or
  `GOOGLE_MAPS_API_KEY`) is not set, discovery falls back to browser-based
  Google Maps scraping via Playwright
- **Remediation reports:** Use the prompt at `prompts/remediation-backlog.md`
  to convert an audit report JSON into a Kanban remediation backlog
