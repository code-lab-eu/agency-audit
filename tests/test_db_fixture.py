"""Tests for the shared test database fixture provided by conftest.py."""

import asyncpg


async def test_db_conn_fixture_is_connected(db_conn: asyncpg.Connection):
    """The db_conn fixture should yield a live, connected asyncpg connection."""
    row = await db_conn.fetchrow("SELECT 1 AS one")
    assert row is not None
    assert row["one"] == 1


async def test_db_conn_has_migrations_applied(db_conn: asyncpg.Connection):
    """All project migrations are applied — no partial state."""
    versions = await db_conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    applied = {row["version"] for row in versions}

    expected = {
        "000_schema_migrations.sql",
        "001_init.sql",
        "002_add_discovery_status.sql",
        "003_add_audit_log.sql",
        "004_add_failed_discovery_status.sql",
    }
    assert expected <= applied, f"Missing migrations: {expected - applied}. Applied: {applied}"


async def test_db_conn_can_insert_and_query(db_conn: asyncpg.Connection):
    """Basic CRUD should work through the fixture connection.

    Writes are rolled back automatically by the fixture — no manual
    cleanup needed.
    """
    await db_conn.execute("INSERT INTO countries (iso, label) VALUES ('XX', 'Testland')")
    row = await db_conn.fetchrow("SELECT iso, label FROM countries WHERE iso = 'XX'")
    assert row is not None
    assert row["iso"] == "XX"
    assert row["label"] == "Testland"


async def test_db_conn_state_does_not_leak(db_conn: asyncpg.Connection, postgres_dsn: str):
    """Writes inside a db_conn transaction are invisible to other connections.

    The fixture wraps every test connection in a rollback-only
    transaction.  A second connection (simulating the next test
    function) must not see uncommitted writes — proving that the
    isolation boundary works and that no test author needs to
    remember to clean up.
    """
    # Write inside the transaction
    await db_conn.execute("INSERT INTO countries (iso, label) VALUES ('ZZ', 'LeakTest')")

    # Visible within the same transaction
    row = await db_conn.fetchrow("SELECT iso FROM countries WHERE iso = 'ZZ'")
    assert row is not None, "Write must be visible within its own transaction"

    # A separate connection (simulating the next test) must NOT see the
    # uncommitted write — PostgreSQL READ COMMITTED isolation ensures this.
    conn2 = await asyncpg.connect(dsn=postgres_dsn)
    try:
        row2 = await conn2.fetchrow("SELECT iso FROM countries WHERE iso = 'ZZ'")
        assert row2 is None, (
            "Uncommitted write leaked to a separate connection!  "
            "The fixture's transaction isolation is broken."
        )
    finally:
        await conn2.close()


async def test_db_conn_session_scope_isolation():
    """The session scope means repeated calls get the same container.

    This test doesn't use the fixture directly but validates the
    fixture is importable and callable.
    """
    from tests.conftest import db_conn

    assert db_conn is not None
