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
  exactly once per session.  Any migration failure propagates as a hard
  error — PostGIS is a hard project dependency and its absence causes
  the fixture to fail.
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

    Any migration failure propagates as a hard error — PostGIS is a
    hard project dependency and there is no graceful degradation path.
    """

    async def _migrate() -> None:
        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await run_migrations(conn)
        finally:
            await conn.close()

    asyncio.run(_migrate())


@pytest.fixture(scope="session")
def _ensure_seed_data(_ensure_migrations: None, postgres_dsn: str) -> None:
    """Seed baseline data (countries + sample cities) once per session.

    Mirrors what ``scripts/seed-test-db.py`` does so tests can rely on
    a known set of countries and cities without depending on external
    setup.  Uses a committed connection (no transaction wrapper) so the
    rows are visible to every test's own connections.
    """
    ROOT = Path(__file__).resolve().parents[1]

    async def _seed() -> None:
        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            countries_sql = (ROOT / "src" / "agency_audit" / "seed" / "countries.sql").read_text(
                encoding="utf-8"
            )
            await conn.execute(countries_sql)
            cities_sql = (ROOT / "tests" / "fixtures" / "cities.sql").read_text(encoding="utf-8")
            await conn.execute(cities_sql)
        finally:
            await conn.close()

    asyncio.run(_seed())


@pytest.fixture
async def db_conn(
    postgres_dsn: str, _ensure_migrations: None, _ensure_seed_data: None
) -> AsyncGenerator[asyncpg.Connection]:
    """Function-scoped asyncpg connection with rollback-only isolation.

    Each test receives a fresh connection inside a transaction that is
    **always rolled back** at teardown, regardless of test outcome.
    This guarantees that no writes from one test function leak into
    another (or into the developer's local database on the non-Docker
    fallback path).

    The container (if any) and migrations are session-scoped so they
    are set up only once.
    """
    conn = await asyncpg.connect(dsn=postgres_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
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
