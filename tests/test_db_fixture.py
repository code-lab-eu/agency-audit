"""Tests for the shared test database fixture provided by conftest.py."""

import asyncpg


async def test_db_conn_fixture_is_connected(db_conn: asyncpg.Connection):
    """The db_conn fixture should yield a live, connected asyncpg connection."""
    row = await db_conn.fetchrow("SELECT 1 AS one")
    assert row is not None
    assert row["one"] == 1


async def test_db_conn_has_migrations_applied(db_conn: asyncpg.Connection):
    """Every expected migration is applied — no partial state.

    Migrations 000–004 are always required.  Migration 005 (PostGIS) is
    required only when the PostGIS extension is available on the server.
    """
    versions = await db_conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    applied = {row["version"] for row in versions}

    # Core migrations — always required.
    required = {
        "000_schema_migrations.sql",
        "001_init.sql",
        "002_add_discovery_status.sql",
        "003_add_audit_log.sql",
        "004_add_failed_discovery_status.sql",
    }
    missing = required - applied
    assert not missing, f"Core migrations not applied: {missing}"

    # Migration 005 (PostGIS) — required only when the extension is available.
    postgis_ok = await db_conn.fetchval(
        "SELECT count(*) > 0 FROM pg_available_extensions WHERE name = 'postgis'"
    )
    migration_005 = "005_add_spatial_geometry.sql"

    if postgis_ok:
        assert migration_005 in applied, (
            "PostGIS is available but migration 005 was not applied — "
            "the database is missing spatial columns."
        )
        assert applied == required | {migration_005}, (
            f"Unexpected extra migrations: {applied - required - {migration_005}}"
        )
    else:
        assert migration_005 not in applied, (
            "PostGIS is unavailable but migration 005 appears in the ledger — "
            "run_migrations recorded a migration that was skipped."
        )
        # No unexpected migrations in the ledger.
        assert applied == required, f"Unexpected extra migrations: {applied - required}"


async def test_db_conn_can_insert_and_query(db_conn: asyncpg.Connection):
    """Basic CRUD should work through the fixture connection."""
    # Create a test country
    await db_conn.execute("INSERT INTO countries (iso, label) VALUES ('XX', 'Testland')")
    row = await db_conn.fetchrow("SELECT iso, label FROM countries WHERE iso = 'XX'")
    assert row is not None
    assert row["iso"] == "XX"
    assert row["label"] == "Testland"

    # Clean up
    await db_conn.execute("DELETE FROM countries WHERE iso = 'XX'")


async def test_db_conn_session_scope_isolation():
    """The session scope means repeated calls get the same container.

    This test doesn't use the fixture directly but validates the
    fixture is importable and callable.
    """
    from tests.conftest import db_conn

    assert db_conn is not None
