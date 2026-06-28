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

## Branch and PR discipline (non-negotiable)

- ALWAYS create a feature branch off master before touching any code:
  `git checkout master && git pull && git checkout -b fix/<slug>` or `feat/<slug>`.
- NEVER push directly to master. Not for small fixes, not for typos, not ever.
- When your work is complete, push your branch and open a pull request against
  master. Do NOT merge the PR yourself — the gate runs, a human reviews.
- A task is NOT complete until the corresponding pull request has been created
  and all required CI checks pass. Work committed to a branch with no PR is
  lost work — it will never be reviewed or merged.
- Use the `gh` CLI for PR creation: `gh pr create --title "..." --body "..."`.

## Prerequisites

- Python **3.14+**
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
# Full QA gate — run this before every push (lint + format check + mypy + tests).
# Single source of truth for "is my branch ready"; mirrors the CI quality gates.
scripts/qa.sh

# Same, but auto-fix lint + formatting first, then re-check
scripts/qa.sh --fix

# Run the test suite (NOT dependent on a live database — pool is mocked)
uv run --extra dev pytest

# Run with coverage
uv run --extra dev pytest --cov=src/agency_audit --cov-report=term

# Individual gates (all run via `uv run --extra dev` so they use the project's
# Python 3.14 interpreter — do NOT run mypy via `uvx`, which defaults to an
# older Python that cannot parse PEP 695 generics and reports false syntax errors).
uv run --extra dev ruff check src/ tests/          # Lint
uv run --extra dev ruff format --check src/ tests/ # Format check (enforce double-quotes)
uv run --extra dev mypy src/                       # Type check

# Apply auto-fixes before committing
uv run --extra dev ruff check --fix src/ tests/
uv run --extra dev ruff format src/ tests/

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
    audit/                    # Website audit logic (7 modules)
      __init__.py             # Public API: audit_website, audit_websites, AuditData, compute_score
      scoring_config.yaml     # Weights for the 0-100 audit score (canonical copy)
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
  scripts/
    qa.sh                     # Full local QA gate (lint + format + mypy + tests); run before every push
  pyproject.toml              # Project metadata, deps, ruff, pytest, mypy config
  docker-compose.yml          # Local PostgreSQL 16
  .github/workflows/agency-audit-ci.yml  # CI: lint, format check, mypy
```

## Code style & conventions

- **Line length:** 100 characters (`pyproject.toml` → `tool.ruff.line-length`)
- **Quote style:** double quotes (enforced by ruff format)
- **Lint rules:** `E, F, I, UP, B, SIM` (pyflakes, import sorting, pyupgrade,
  flake8-bugbear, code simplification)
- **Imports:** `from __future__ import annotations` at the top of every
  type-annotated module (targets Python ≥3.14)
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
- **Before pushing:** run `scripts/qa.sh` — it must exit green. This runs all
  CI quality gates (ruff lint, ruff format check, mypy) plus the test suite in
  one shot, so a green run here means CI should pass too

## Contribution workflow

Multiple agents work in parallel, so every branch must start from — and stay
current with — the latest remote `master`. Stale branches are the main source of
merge conflicts.

1. Fetch first, then branch off the **latest remote** `master`:
   `git fetch origin && git checkout -b feat/<slug> origin/master`
   (use `fix/<slug>` for fixes). In a worktree:
   `git fetch origin && git worktree add -b feat/<slug> <path> origin/master`.
2. Make changes, write tests, run the full QA gate (`scripts/qa.sh`)
3. Commit with [conventional commit](https://www.conventionalcommits.org/)
   prefixes (`feat:`, `fix:`, `test:`, `refactor:`)
4. **Re-sync before pushing.** `master` may have moved while you worked:
   `git fetch origin && git merge origin/master`, resolve any conflicts, then
   re-run `scripts/qa.sh` before continuing.
5. Push your branch and open a PR against `master` — CI runs ruff lint,
   ruff format check, and mypy.  A human reviews before merge
6. **If the PR reports conflicts with `master`**, merge master into your branch:
   `git fetch origin && git merge origin/master` (resolve conflicts, re-run the
   suite) then `git push origin <branch-name>`. Do NOT rebase and force-push.
7. Never push directly to `master`

## Force-push avoidance

**Never rebase, squash, amend, or otherwise rewrite pushed commits on a
shared branch.** Force-pushes (`git push --force`, `git push --force-with-lease`,
`git push --force-if-includes`) require manual approval on this repository and
block progress until a human reviews and grants permission.

Use these alternatives instead:

- **Merge conflicts:** Pull the latest `master` and merge it into your branch
  (`git fetch origin && git merge origin/master`). Resolve conflicts, re-run
  the full test suite, and push normally with `git push origin <branch-name>`.
- **Fixing a mistake in the latest commit:** Make a new commit rather than
  amending the pushed one. Use `git commit --fixup <sha>` and squash during
  merge at PR time.
- **Accidentally committed to the wrong branch:** Create a new branch from the
  current HEAD (`git checkout -b <correct-branch>`), then push the new branch.
  The old branch will be cleaned up at PR merge.

If you inadvertently create a situation that would require force-push (e.g. a
sensitive key in a pushed commit, a large file that should not be in the repo),
do not proceed — seek human help or abandon the current branch and recreate
the changes on a fresh branch without rewriting history on the shared one.

## Additional context

- **Scoring:** Weights live in `src/agency_audit/audit/scoring_config.yaml`
  (loaded at runtime). The file ships inside the wheel. A repo-root copy is
  also checked for convenience during development.
  Adjust weights there — no code changes needed for tuning
- **MCP server:** Exposes 5 tools (`get_next_city`, `report_website`,
  `get_unaudited_website`, `submit_audit`, `get_stats`) for agent-driven
  operation.  Run with `uv run agency-audit-mcp`
- **Discovery fallback:** If `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` (or
  `GOOGLE_MAPS_API_KEY`) is not set, discovery falls back to browser-based
  Google Maps scraping via Playwright
- **Remediation reports:** Use the prompt at `prompts/remediation-backlog.md`
  to convert an audit report JSON into a Kanban remediation backlog
