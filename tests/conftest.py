"""Shared pytest fixtures for agency-audit.

Provides a function-scoped ``db_conn`` fixture for integration tests that
require a live PostgreSQL database.

Architecture
------------
- ``postgres_dsn`` (session, sync) — starts a disposable PostgreSQL 16
  container via testcontainers when Docker is available, otherwise falls
  back to a local PostgreSQL instance configured via ``AGENCY_AUDIT_*``
  environment variables.  When Docker is used, the image is
  ``postgres:16-alpine``.
- ``_ensure_migrations`` (session, sync) — applies all project migrations
  exactly once per session.  Any migration failure propagates as a hard
  error.
- ``db_conn`` (function, async) — creates a fresh asyncpg connection for
  each test function, bound to the test's own event loop with migrations
  already applied.  Rollback-only isolation; the default for most tests.
- ``_db_template`` (session, sync) + ``fresh_db`` (function, async) — for
  tests that exercise production code opening its own pool via
  ``get_pool()`` (web app, discovery pipeline, loop), where rollback
  isolation does not reach.  ``fresh_db`` clones a schema-only session
  template into a private, uniquely named database, redirects
  ``get_pool()`` onto it, applies the overridable ``seed_reference`` /
  ``seed_scenario`` fixtures, and drops the database on teardown.

This design avoids event-loop mismatch errors and connection-concurrency
issues while keeping session-scoped container startup (one container per
test session, many test functions).
"""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from agency_audit import db as _db
from agency_audit.config import settings
from agency_audit.migrations import run_migrations

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
COUNTRIES_SQL = ROOT / "src" / "agency_audit" / "seed" / "countries.sql"
CITIES_SQL = ROOT / "tests" / "fixtures" / "cities.sql"


def _dsn_with_db(dsn: str, dbname: str) -> str:
    """Return ``dsn`` with its database name swapped for ``dbname``."""
    return urlunparse(urlparse(dsn)._replace(path=f"/{dbname}"))


async def _reset_pool() -> None:
    """Drop the process-wide asyncpg pool so the next ``get_pool()`` rebuilds it.

    ``close_pool()`` closes gracefully when the pool lives on the current
    event loop.  For a pool created on ``TestClient``'s anyio loop (already
    closed by the time we tear down) ``close_pool()`` raises — we then just
    drop the module-level references; ``DROP DATABASE ... WITH (FORCE)``
    terminates any server-side backends the orphaned pool still holds.
    """
    try:
        await _db.close_pool()
    except Exception:
        _db._pool = None
        _db._pool_closed = True


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

        container = PostgresContainer("postgres:16-alpine")
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

    Any migration failure propagates as a hard error.
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
    """Reset the database to a known, deterministic baseline once per session.

    Clears every mutable table (discovery_log, website_cities, websites,
    cities), resets country active flags, then loads the canonical seed
    data.  Uses a committed connection so the clean slate is visible to
    every test's own connections — no ambient data can leak into
    assertions.

    After this fixture, the database contains exactly:

    * 44 countries (4 active: BE, BG, ES, RS)
    * 20 cities (all BG, all ``discovery_status = 'pending'``)
    * 0 websites, 0 website_cities rows, 0 discovery_log rows
    """
    ROOT = Path(__file__).resolve().parents[1]

    async def _seed() -> None:
        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            # Phase 1: Clean — order respects FK constraints so no
            # cascades mask missing dependent rows.  Delete countries
            # (not just flip active flags) so the seed INSERT below
            # creates fresh rows with the correct active values.
            await conn.execute("DELETE FROM discovery_log")
            await conn.execute("DELETE FROM website_cities")
            await conn.execute("DELETE FROM websites")
            await conn.execute("DELETE FROM cities")
            await conn.execute("DELETE FROM countries")

            # Phase 2: Seed the canonical data.
            countries_sql = (ROOT / "src" / "agency_audit" / "seed" / "countries.sql").read_text(
                encoding="utf-8"
            )
            await conn.execute(countries_sql)
            cities_sql = (ROOT / "tests" / "fixtures" / "cities.sql").read_text(encoding="utf-8")
            await conn.execute(cities_sql)
        finally:
            await conn.close()

    asyncio.run(_seed())


# ---------------------------------------------------------------------------
# Per-test disposable database (the ``fresh_db`` fixture)
#
# Rollback isolation (``db_conn``) is the default and covers most tests.  It
# cannot help tests that exercise production code which opens its *own* pool
# via ``get_pool()`` (the web app, the discovery pipeline, the loop): those
# connections never join the test's transaction.  For those, ``fresh_db``
# hands each test a private database cloned from a session template, with the
# process-wide pool redirected onto it, and drops it on teardown.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _db_template(postgres_dsn: str) -> Generator[str]:
    """Build a schema-only template database once per session.

    The template holds the slow, invariant part — all migrations — so each
    per-test clone is a fast file copy
    (``CREATE DATABASE ... TEMPLATE``) rather than a fresh migration run.
    Seed data is applied per test (see ``seed_reference``) so it stays
    overridable.  Nothing may connect to the template while clones are
    taken, so the build connection is closed before yielding.
    """
    template_name = f"aa_tmpl_{uuid.uuid4().hex[:8]}"

    async def _build() -> None:
        maint = await asyncpg.connect(dsn=postgres_dsn)
        try:
            # Sweep per-test databases orphaned by a previously crashed run
            # (harmless in a disposable container; matters on a local DB).
            stale = await maint.fetch(
                "SELECT datname FROM pg_database WHERE datname LIKE 'aa_test_%'"
            )
            for row in stale:
                await maint.execute(f'DROP DATABASE IF EXISTS "{row["datname"]}" WITH (FORCE)')
            await maint.execute(f'CREATE DATABASE "{template_name}"')
        finally:
            await maint.close()

        build = await asyncpg.connect(dsn=_dsn_with_db(postgres_dsn, template_name))
        try:
            await run_migrations(build)
        finally:
            await build.close()

    asyncio.run(_build())
    yield template_name

    async def _drop() -> None:
        maint = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await maint.execute(f'DROP DATABASE IF EXISTS "{template_name}" WITH (FORCE)')
        finally:
            await maint.close()

    asyncio.run(_drop())


# ``seed_reference`` and ``seed_scenario`` are the overridable setup seams:
# a test module redefines either fixture to return a different coroutine and
# ``fresh_db`` will run that instead — mirroring an xUnit ``setUp`` override.
SeedFn = Callable[[asyncpg.Connection], Awaitable[None]]


@pytest.fixture
def seed_reference() -> SeedFn:
    """Default reference-data seed: the canonical countries + BG cities.

    Override in a test/module to control the reference baseline, e.g. to
    seed exactly N countries for a deterministic aggregate assertion.
    """

    async def _seed(conn: asyncpg.Connection) -> None:
        await conn.execute(COUNTRIES_SQL.read_text(encoding="utf-8"))
        await conn.execute(CITIES_SQL.read_text(encoding="utf-8"))

    return _seed


@pytest.fixture
def seed_scenario() -> SeedFn:
    """Default scenario seed: none — the mutable tables start empty.

    Override to insert the exact websites / links / logs a test needs.
    """

    async def _seed(conn: asyncpg.Connection) -> None:
        return None

    return _seed


@pytest.fixture
async def fresh_db(
    _db_template: str,
    postgres_dsn: str,
    seed_reference: SeedFn,
    seed_scenario: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[asyncpg.Connection]:
    """Function-scoped private database cloned from the session template.

    Clones the template into a uniquely named database, points
    ``settings`` (and therefore ``get_pool()``) at it, applies the
    overridable seed fixtures, and yields a live connection for the test
    to seed or assert against.  On teardown the pool is dropped and the
    database is removed — no manual cleanup, and exact-count assertions
    are safe because the database is pristine.
    """
    db_name = f"aa_test_{uuid.uuid4().hex[:12]}"

    maint = await asyncpg.connect(dsn=postgres_dsn)
    try:
        await maint.execute(f'CREATE DATABASE "{db_name}" TEMPLATE "{_db_template}"')
    finally:
        await maint.close()

    fresh_dsn = _dsn_with_db(postgres_dsn, db_name)
    parsed = urlparse(fresh_dsn)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "localhost")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", db_name)
    monkeypatch.setattr(settings, "pg_user", parsed.username or "")
    monkeypatch.setattr(settings, "pg_password", parsed.password or "")
    await _reset_pool()

    conn = await asyncpg.connect(dsn=fresh_dsn)
    try:
        await seed_reference(conn)
        await seed_scenario(conn)
        yield conn
    finally:
        await conn.close()
        await _reset_pool()
        maint = await asyncpg.connect(dsn=postgres_dsn)
        try:
            await maint.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        finally:
            await maint.close()


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
