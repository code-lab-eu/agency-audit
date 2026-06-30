"""Shared pytest fixtures for agency-audit.

Provides a function-scoped ``db_conn`` fixture for integration tests that
require a live PostgreSQL database.

Architecture
------------
- ``postgres_dsn`` (session, sync) — starts a disposable PostgreSQL 16
  container via testcontainers when Docker is available, otherwise falls
  back to a local PostgreSQL instance configured via ``AGENCY_AUDIT_*``
  environment variables.  When Docker is used, the image is
  ``postgis/postgis:16-3.4-alpine`` so the PostGIS extension is included.
- ``_ensure_migrations`` (session, sync) — applies all project migrations
  exactly once per session.  Migration 005 (PostGIS spatial geometry) is
  explicitly skipped when the extension is not available on the server;
  **any other migration failure propagates as a hard error** — a
  partially migrated database is never yielded.
- ``db_conn`` (function, async) — creates a fresh asyncpg connection for
  each test function, bound to the test's own event loop with migrations
  already applied.

This design avoids event-loop mismatch errors and connection-concurrency
issues while keeping session-scoped container startup (one container per
test session, many test functions).
"""

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.migrations import run_migrations

logger = logging.getLogger(__name__)


def _docker_available() -> bool:
    """Check whether the Docker daemon socket is reachable."""
    socket_path = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if socket_path.startswith("unix://"):
        return os.path.exists(socket_path.removeprefix("unix://"))
    return True  # remote TCP - assume reachable


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """Session-scoped PostgreSQL connection DSN.

    Uses AGENCY_AUDIT_PG_* env vars when explicitly configured (e.g. CI
    service containers), falls back to testcontainers when Docker is
    available, otherwise uses localhost defaults.
    """
    if os.environ.get("AGENCY_AUDIT_PG_HOST"):
        return settings.dsn

    if _docker_available():
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgis/postgis:16-3.4-alpine")
        container.start()
        postgres_dsn._tc_container = container  # type: ignore[attr-defined]
        # testcontainers returns "postgresql+psycopg2://..."; asyncpg
        # requires a plain "postgresql://" scheme.
        raw = container.get_connection_url()
        return raw.replace("postgresql+psycopg2://", "postgresql://")

    return settings.dsn


@pytest.fixture(scope="session")
def _ensure_migrations(postgres_dsn: str) -> None:
    """Apply all project migrations exactly once per test session.

    Uses ``asyncio.run`` so the temporary connection lives in its own
    event loop — no interference with the test functions' event loops.

    Migration 005 (PostGIS) is skipped when the extension is not
    available on the database server.  **Any other migration failure
    propagates as a hard error** — a partially migrated database is
    never yielded.
    """

    async def _migrate() -> None:
        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            # Check whether the PostGIS extension is available before
            # attempting migration 005, so we never yield a partially
            # migrated database.
            postgis_available = await conn.fetchval(
                "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'postgis'"
            )
            if not postgis_available:
                logger.warning(
                    "PostGIS extension not available — "
                    "migration 005_add_spatial_geometry.sql skipped. "
                    "Spatial columns (websites.location) will not exist. "
                    "Use a PostGIS-capable image "
                    "(postgis/postgis:16-3.4-alpine) or install PostGIS "
                    "on the local server."
                )
                # Run migrations 000–004 only, leaving 005 unapplied.
                # Any failure here propagates (no blanket except).
                src_migrations = (
                    Path(__file__).resolve().parent.parent / "src" / "agency_audit" / "migrations"
                )
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp = Path(tmpdir)
                    for f in sorted(src_migrations.glob("*.sql")):
                        if "spatial" not in f.name:
                            shutil.copy(f, tmp / f.name)
                    await run_migrations(conn, migrations_dir=tmp)
            else:
                # Run all migrations — any failure propagates.
                await run_migrations(conn)
        finally:
            await conn.close()

    asyncio.run(_migrate())


@pytest.fixture
async def db_conn(
    postgres_dsn: str, _ensure_migrations: None
) -> AsyncGenerator[asyncpg.Connection]:
    """Function-scoped asyncpg connection with migrations already applied.

    Each test receives a fresh connection.  The container (if any) and
    migrations are session-scoped so they are set up only once.
    """
    conn = await asyncpg.connect(dsn=postgres_dsn)
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Session teardown - stop the testcontainers container so it doesn't linger
# after all tests finish.
# ---------------------------------------------------------------------------
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Stop the PostgresContainer if one was created during this session."""
    container = getattr(postgres_dsn, "_tc_container", None)
    if container is not None:
        container.stop()
