# Agency Audit

> Real Estate Radar — Website Discovery & Audit System.

Agency Audit discovers, audits, and ranks real estate agency websites across
44 European countries for inclusion in the
[Real Estate Radar](https://www.realestateradar.eu/) index.

## Overview

The system runs as a single operational pipeline, one country at a time:

1. **Discover** — find agency websites via the Google Maps Places API.
2. **Audit** — run each website through a 7-module audit pipeline and score it.
3. **QC** — flag low-confidence results for review.
4. **Re-audit** — re-run flagged or stale websites.

Results are stored in PostgreSQL and browsable through a built-in FastAPI +
HTMX dashboard.

## Quick Start

The project runs as a Docker Compose stack: a PostgreSQL database and the
agency-audit application serving the dashboard.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the Compose plugin

### Run

1. Configure your environment:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` to set your Google Maps API key and any other values you want
   to override. See [Configuration](#configuration) for the full list.

2. Build the images and start the stack:

   ```bash
   docker compose up -d --build
   ```

   The dashboard is available at <http://localhost:8000>. It applies the
   database migrations when it starts.

3. Load the reference data:

   ```bash
   docker compose exec app agency-audit seed-countries
   docker compose exec app agency-audit import-geonames
   ```

4. Verify everything is running:

   ```bash
   curl http://localhost:8000/health
   docker compose exec app agency-audit stats
   ```

### Podman Quadlet

**Prerequisites:** Linux with systemd and Podman 6.1 or newer.

The unit files are in `quadlet/`: the image build, the application, the
PostgreSQL container with its volume, and their network. Only the dashboard is
published, on `127.0.0.1:8000`; the database is not reachable from the host.

1. Create the database password and the Google Maps API key as podman secrets:
   ```bash
   openssl rand -hex 32 | podman secret create agency-audit-db-password -
   read -rs KEY && printf %s "$KEY" | podman secret create agency-audit-google-maps-api-key -
   ```

2. Link the unit directory into the Podman configuration of your user, load the
   units and start the stack. The first start builds the image:
   ```bash
   mkdir -p ~/.config/containers/systemd
   ln -s "$PWD/quadlet" ~/.config/containers/systemd/agency-audit
   systemctl --user daemon-reload
   systemctl --user start agency-audit
   ```
   Quadlet follows the link and resolves the build context `..` of
   `agency-audit.build` against the repository. `podman quadlet install` copies
   the units without the repository, so do not use it here.

3. Load the reference data:
   ```bash
   podman exec agency-audit agency-audit seed-countries
   podman exec agency-audit agency-audit import-geonames
   ```

The stack starts at boot when systemd starts your user instance at boot:
`loginctl enable-linger "$USER"`.

To publish the dashboard on another address or port, create
`~/.config/containers/systemd/agency-audit.container.d/settings.conf` and run
`systemctl --user daemon-reload`. The empty `PublishPort=` removes the default:

```ini
[Container]
PublishPort=
PublishPort=127.0.0.1:8123:8000
```

## Configuration

All configuration is read from environment variables prefixed with
`AGENCY_AUDIT_`. Copy `.env.example` to `.env` and fill in your values:

| Variable | Default | Description |
|---|---|---|
| `AGENCY_AUDIT_PG_HOST` | `localhost` | PostgreSQL host |
| `AGENCY_AUDIT_PG_PORT` | `5432` | PostgreSQL port |
| `AGENCY_AUDIT_PG_USER` | `agency_audit` | PostgreSQL user |
| `AGENCY_AUDIT_PG_PASSWORD` | *(empty)* | PostgreSQL password |
| `AGENCY_AUDIT_PG_DATABASE` | `agency_audit` | PostgreSQL database |
| `AGENCY_AUDIT_GEONAMES_MIN_POPULATION` | `50000` | Minimum population for city import |
| `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` | *(empty)* | Google Maps API key (Places + Geocoding), **required** for the `discover` command |

The Docker Compose stack sets the database variables automatically; you only
need to provide `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` to enable discovery.

### Google Maps API key

Discovery uses two Google Maps Platform APIs on a single key: the **Places
API** (finding agencies) and the **Geocoding API** (resolving a city name to a
bounding box). To set the key up:

1. In the [Google Cloud Console](https://console.cloud.google.com/), select or
   create a project with billing enabled.
2. Under **APIs & Services → Enabled APIs & services**, enable both the
   **Places API** and the **Geocoding API** for that project.
3. Under **APIs & Services → Credentials**, create an API key (or reuse an
   existing one).
4. If the key has **API restrictions** set to "Restrict key", add both the
   Places API and the Geocoding API to its allowed list. (Enabling an API for
   the project is not enough on its own — a restricted key must also list it.)
5. Set the key as `AGENCY_AUDIT_GOOGLE_MAPS_API_KEY` in your `.env`.

Restriction changes can take a few minutes to propagate. If a request returns
`REQUEST_DENIED`, the API is either not enabled for the key's project or not in
the key's allowed API list.

## Usage

Run any command inside the running stack by prefixing it with
`docker compose exec app`. The examples below show the bare commands.

### Discover agencies

Finds real estate agencies via the Google Maps Places API. Requires
`AGENCY_AUDIT_GOOGLE_MAPS_API_KEY`.

```bash
# Discover agencies in Bulgaria (limit to 5 cities)
agency-audit discover --country BG --max-cities 5

# Discover across multiple countries
agency-audit discover --countries "BG,RO,GR" --max-cities 3
```

### Audit websites

Run the full 7-module audit pipeline on a URL or a stored database record.

```bash
# Audit a URL directly (table output)
agency-audit audit --url "https://example-agency.com"

# JSON output
agency-audit audit --url "https://example-agency.com" --output json

# Audit a website already in the database and store the results
agency-audit audit --website-id 42 --output db

# Batch-audit multiple URLs concurrently
agency-audit batch-audit --urls "https://a.bg,https://b.bg" --concurrency 3
```

### Run the full pipeline

Execute the complete **discover → audit → QC → re-audit** loop for a country:

```bash
# One country, default settings
agency-audit run --country BG

# Custom concurrency and city limit
agency-audit run --country BG --max-cities 10 --concurrency 5

# Skip phases
agency-audit run --country BG --skip-discovery --skip-qc

# Every active country, sequentially
agency-audit run-all

# Limit run-all to specific countries
agency-audit run-all --countries "BG,RO,GR"
```

### Web dashboard

Start the FastAPI + HTMX dashboard.

```bash
# Default: http://127.0.0.1:8000
agency-audit serve

# Custom host, port, and auto-reload
agency-audit serve --host 127.0.0.1 --port 8080 --reload
```

### Supporting commands

```bash
# Database statistics
agency-audit stats

# Pipeline progress
agency-audit progress

# Quality control
agency-audit qc --action run
agency-audit qc --action list-review

# Re-audit queue
agency-audit reaudit --action queue --country BG
agency-audit reaudit --action trigger --limit 50
```

## Development

Install the project with its dev dependencies (ruff, pytest, mypy):

```bash
uv sync --extra dev
```

### Quality gate

`scripts/qa.sh` runs lint, format check, type check, and tests with coverage
in a single pass — the same checks CI runs. If it is green locally, CI should
pass too.

```bash
# All-in-one (mirrors CI)
scripts/qa.sh

# Auto-apply fixes, then re-check
scripts/qa.sh --fix
```

Or run each check individually:

```bash
uv run ruff check src/ tests/            # Lint
uv run ruff format --check src/ tests/   # Format check
uv run mypy src/                         # Type check
uv run --extra dev pytest                # Test suite
```

To auto-fix lint and formatting:

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
```

### Test data

`src/agency_audit/scripts/seed_test_data.py` populates the database with
sample agencies and audit records for dashboard development:

```bash
uv run python src/agency_audit/scripts/seed_test_data.py
```

It inserts 5 sample websites (3 audited with scores, 1 blocked, 1 pending)
linked to Bulgarian cities, and requires a migrated and seeded database. The
equivalent setup for CI lives in `scripts/seed-test-db.py`.

## Tech stack

- **Language:** Python 3.14+
- **CLI:** Typer + Rich
- **Web:** FastAPI, Jinja2, HTMX
- **HTTP & parsing:** httpx, selectolax, Playwright
- **Data:** PostgreSQL 16+ (JSONB for flexible audit data), asyncpg
- **Agent bridge:** FastMCP
- **Tooling:** uv (packaging), ruff (lint/format), mypy (types), pytest (tests)
