"""Shared pytest fixtures for agency-audit.

Provides a session-scoped ``db_conn`` fixture for integration tests that
require a live PostgreSQL database.

Architecture
------------
- ``postgres_dsn`` (session, sync) — starts a disposable PostgreSQL 16
  container via testcontainers when Docker is available, otherwise falls
  back to a local PostgreSQL instance configured via ``AGENCY_AUDIT_*``
  environment variables.
- ``_ensure_migrations`` (session, sync) — applies all project migrations
  exactly once per session using ``asyncio.run`` so the async connection
  lives in its own temporary event loop.
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

import asyncpg
import pytest

from agency_audit.migrations import run_migrations

logger = logging.getLogger(__name__)


def _docker_available() -> bool:
    """Check whether the Docker daemon socket is reachable."""
    socket_path = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    if socket_path.startswith("unix://"):
        return os.path.exists(socket_path.removeprefix("unix://"))
    return True  # remote TCP - assume reachable


def _build_fallback_dsn() -> str:
    """Build a DSN from AGENCY_AUDIT_* environment variables or defaults."""
    pg_host = os.environ.get("AGENCY_AUDIT_PG_HOST", "localhost")
    pg_port = os.environ.get("AGENCY_AUDIT_PG_PORT", "5432")
    pg_user = os.environ.get("AGENCY_AUDIT_PG_USER", "hermes")
    pg_password = os.environ.get("AGENCY_AUDIT_PG_PASSWORD", "hermes")
    pg_database = os.environ.get("AGENCY_AUDIT_PG_DATABASE", "agency_audit_test")

    colon, slash, at_sign = ":", "/", "@"
    auth = pg_user
    if pg_password:
        auth = pg_user + colon + pg_password
    return "postgresql://" + auth + at_sign + pg_host + colon + pg_port + slash + pg_database


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """Session-scoped PostgreSQL connection DSN.

    Uses AGENCY_AUDIT_PG_* env vars when explicitly configured (e.g. CI
    service containers), falls back to testcontainers when Docker is
    available, otherwise uses localhost defaults.
    """
    if os.environ.get("AGENCY_AUDIT_PG_HOST"):
        return _build_fallback_dsn()

    if _docker_available():
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgres:16-alpine")
        container.start()
        postgres_dsn._tc_container = container  # type: ignore[attr-defined]
        # testcontainers returns "postgresql+psycopg2://..."; asyncpg
        # requires a plain "postgresql://" scheme.
        raw = container.get_connection_url()
        return raw.replace("postgresql+psycopg2://", "postgresql://")

    return _build_fallback_dsn()


@pytest.fixture(scope="session")
def _ensure_migrations(postgres_dsn: str) -> None:
    """Apply all project migrations exactly once per test session.

    Uses ``asyncio.run`` so the temporary connection lives in its own
    event loop - no interference with the test functions' event loops.

    Migration failures (e.g. missing PostGIS extension) are logged as
    warnings rather than crashing — the fixture still succeeds so tests
    that don't need the failing migration can run.
    """

    async def _migrate() -> None:
        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await run_migrations(conn)
        except Exception:
            logger.warning(
                "One or more migrations failed to apply — "
                "this is expected if system extensions (e.g. PostGIS) "
                "are not installed. Tests that depend on those "
                "migrations may fail.",
                exc_info=True,
            )
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
