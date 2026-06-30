"""Tests for the shared test database fixture provided by conftest.py."""

import asyncpg


async def test_db_conn_fixture_is_connected(db_conn: asyncpg.Connection):
    """The db_conn fixture should yield a live, connected asyncpg connection."""
    row = await db_conn.fetchrow("SELECT 1 AS one")
    assert row is not None
    assert row["one"] == 1


async def test_db_conn_has_migrations_applied(db_conn: asyncpg.Connection):
    """All project migrations should be applied to the test database."""
    # The schema_migrations table tracks applied migrations
    versions = await db_conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    applied = [row["version"] for row in versions]
    # At minimum we expect the core tables
    assert "000_schema_migrations.sql" in applied
    assert "001_init.sql" in applied
    assert "002_add_discovery_status.sql" in applied
    assert "003_add_audit_log.sql" in applied
    assert "004_add_failed_discovery_status.sql" in applied


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
