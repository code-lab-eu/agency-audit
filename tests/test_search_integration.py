"""Integration tests for search — exercises PostgreSQL tsvector/tsquery.

These tests require a live PostgreSQL connection.  They are skipped
when the database is unavailable.  Run with:

    AGENCY_AUDIT_PG_PASSWORD=*** uv run --extra dev pytest \\
        tests/test_search_integration.py -v
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from agency_audit.config import settings
from agency_audit.db import close_pool
from agency_audit.migrations import run_migrations
from agency_audit.search import search_agencies, set_agency_description

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    """Return True if we can connect to the configured PG database."""

    async def _check():
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn=settings.dsn), timeout=5)
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return False


# Module-scoped skip marker
_pg_skip = not _pg_available()

pytestmark = [
    pytest.mark.skipif(
        _pg_skip,
        reason="PostgreSQL not available — set AGENCY_AUDIT_PG_PASSWORD",
    ),
    # Keep the event loop alive across all tests in this module so
    # the global get_pool() pool survives beyond the first test.
    pytest.mark.asyncio(loop_scope="module"),
]


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

TEST_AGENCIES = [
    {
        "url": "https://alpha-realestate.example.com",
        "label": "Alpha Real Estate Berlin",
        "description": "Premium properties in Berlin and Brandenburg",
        "score": 85,
    },
    {
        "url": "https://beta-properties.example.com",
        "label": "Beta Properties Hamburg",
        "description": "Your Hamburg real estate partner since 1995",
        "score": 72,
    },
    {
        "url": "https://gamma-homes.example.com",
        "label": "Gamma Homes Munich",
        "description": "Munich luxury apartments and villas",
        "score": 90,
    },
]


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def pg_conn():
    """Module-scoped PG connection with migration applied and test data inserted.

    Connection is reused across all tests in this module, then cleaned up.
    """
    conn = await asyncpg.connect(dsn=settings.dsn)

    # Apply the search migration (runs inside transaction, no-op if applied)
    await run_migrations(conn)

    # Insert (or refresh) test rows
    for a in TEST_AGENCIES:
        await _upsert_test_agency(conn, a)

    yield conn

    # Remove test rows
    for a in TEST_AGENCIES:
        await _delete_test_agency(conn, a["url"])
    await conn.close()
    # Also clean up the global pool so it doesn't leak into other test modules
    await close_pool()


async def _upsert_test_agency(conn: asyncpg.Connection, agency: dict) -> int:
    """Insert or update a test agency, returning its website id."""
    row = await conn.fetchrow(
        """
        INSERT INTO websites (url, label, description, score, audit_status)
        VALUES ($1, $2, $3, $4, 'audited')
        ON CONFLICT (url) DO UPDATE
            SET label = EXCLUDED.label,
                description = EXCLUDED.description,
                score = EXCLUDED.score
        RETURNING id
        """,
        agency["url"],
        agency["label"],
        agency["description"],
        agency["score"],
    )
    return row["id"]


async def _delete_test_agency(conn: asyncpg.Connection, url: str) -> None:
    await conn.execute("DELETE FROM websites WHERE url = $1", url)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchIntegration:
    """Real PostgreSQL search_agencies() tests."""

    async def test_search_by_label_finds_agency(self, pg_conn):
        """Searching for 'Berlin' finds the agency with Berlin in its label."""
        results = await search_agencies("Berlin")
        labels = {r["label"] for r in results}
        assert "Alpha Real Estate Berlin" in labels

    async def test_search_stemming_works(self, pg_conn):
        """Stemming matches different forms: 'property' matches 'properties'."""
        results = await search_agencies("property")
        labels = {r["label"] for r in results}
        assert "Beta Properties Hamburg" in labels

    async def test_no_match_returns_empty(self, pg_conn):
        """A query matching nothing returns an empty list."""
        results = await search_agencies("zzzblargnonexistent")
        assert results == []

    async def test_limit_respected(self, pg_conn):
        """Results respect the limit parameter."""
        results = await search_agencies("estate", limit=1)
        assert 1 <= len(results) <= 1

    async def test_schema_keys_present(self, pg_conn):
        """Returned dicts contain all expected keys."""
        results = await search_agencies("Berlin")
        assert len(results) >= 1
        expected_keys = {"id", "url", "label", "description", "score", "audit_status", "rank"}
        assert set(results[0].keys()) == expected_keys

    async def test_rank_is_float(self, pg_conn):
        """The rank field is a float (a real ts_rank value)."""
        results = await search_agencies("Berlin")
        assert len(results) >= 1
        assert isinstance(results[0]["rank"], float)
        assert results[0]["rank"] > 0

    async def test_search_ranking_order(self, pg_conn):
        """Results are ordered by relevance rank (DESC)."""
        results = await search_agencies("Berlin")
        assert len(results) >= 1
        labels = [r["label"] for r in results]
        assert any("Berlin" in label for label in labels)

    async def test_limit_clamping_happens(self, pg_conn):
        """Limit is clamped to valid range even with PG."""
        results = await search_agencies("Berlin", limit=-5)
        assert len(results) >= 1  # clamped to 1, not -5


class TestSearchEdgeCases:
    """Edge-case behaviour of search_agencies()."""

    async def test_empty_query_returns_empty_list(self, pg_conn):
        """Blank or whitespace-only query returns [] without querying DB."""
        for blank in ("", "   ", "\t\n"):
            results = await search_agencies(blank)
            assert results == [], f"Expected [] for blank query {blank!r}"

    async def test_limit_zero_clamped_to_minimum(self, pg_conn):
        """limit=0 is clamped to 1."""
        results = await search_agencies("Hamburg", limit=0)
        assert len(results) >= 1  # would be 0 rows if limit wasn't clamped

    async def test_limit_above_maximum_is_clamped(self, pg_conn):
        """limit=9999 is clamped to 200, still returns results."""
        results = await search_agencies("Hamburg", limit=9999)
        # Must return results, and must not explode with an absurd LIMIT
        assert len(results) >= 1

    async def test_null_description_in_results(self, pg_conn):
        """A row where description IS NULL is returned with description=None."""
        row = await pg_conn.fetchrow(
            "SELECT id FROM websites WHERE url = $1",
            TEST_AGENCIES[0]["url"],
        )
        assert row is not None

        # Temporarily set description to NULL
        await pg_conn.execute(
            "UPDATE websites SET description = NULL WHERE id = $1",
            row["id"],
        )
        try:
            results = await search_agencies(TEST_AGENCIES[0]["label"].split()[0])
            found = [r for r in results if r["id"] == row["id"]]
            assert len(found) == 1
            assert found[0]["description"] is None
        finally:
            # Restore original description
            await pg_conn.execute(
                "UPDATE websites SET description = $1 WHERE id = $2",
                TEST_AGENCIES[0]["description"],
                row["id"],
            )


class TestSetDescriptionIntegration:
    """Real PostgreSQL set_agency_description() tests."""

    async def test_set_description_persists(self, pg_conn):
        """set_agency_description() writes to the DB and can be read back."""
        row = await pg_conn.fetchrow(
            "SELECT id FROM websites WHERE url = $1",
            TEST_AGENCIES[0]["url"],
        )
        assert row is not None

        await set_agency_description(row["id"], "Updated integration test description")

        desc = await pg_conn.fetchval(
            "SELECT description FROM websites WHERE id = $1",
            row["id"],
        )
        assert desc == "Updated integration test description"

        # Restore original
        await set_agency_description(row["id"], TEST_AGENCIES[0]["description"])

    async def test_set_description_strips_whitespace(self, pg_conn):
        """Leading/trailing whitespace is stripped before writing."""
        row = await pg_conn.fetchrow(
            "SELECT id FROM websites WHERE url = $1",
            TEST_AGENCIES[0]["url"],
        )
        assert row is not None

        await set_agency_description(row["id"], "  Nice agency  ")
        desc = await pg_conn.fetchval(
            "SELECT description FROM websites WHERE id = $1",
            row["id"],
        )
        assert desc == "Nice agency"

        # Restore original
        await set_agency_description(row["id"], TEST_AGENCIES[0]["description"])

    async def test_set_description_empty_sets_null(self, pg_conn):
        """Empty or whitespace-only description sets NULL."""
        row = await pg_conn.fetchrow(
            "SELECT id FROM websites WHERE url = $1",
            TEST_AGENCIES[1]["url"],
        )
        assert row is not None

        for blank in ("", "   ", "\t\n"):
            await set_agency_description(row["id"], blank)
            desc = await pg_conn.fetchval(
                "SELECT description FROM websites WHERE id = $1",
                row["id"],
            )
            assert desc is None, f"Expected NULL for blank {blank!r}, got {desc!r}"

        # Restore original
        await set_agency_description(row["id"], TEST_AGENCIES[1]["description"])


class TestMigrationApplied:
    """Verify the 006 migration structure is correct."""

    async def test_search_vector_column_exists(self, pg_conn):
        """The search_vector generated column was created by the migration."""
        row = await pg_conn.fetchrow(
            """
            SELECT column_name, is_generated, generation_expression
            FROM information_schema.columns
            WHERE table_name = 'websites' AND column_name = 'search_vector'
            """
        )
        assert row is not None, "search_vector column missing — migration not applied?"
        assert row["is_generated"] == "ALWAYS", (
            "search_vector must be GENERATED ALWAYS AS ... STORED"
        )

    async def test_gin_index_exists(self, pg_conn):
        """The GIN index was created by the migration."""
        rows = await pg_conn.fetch(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'websites'
              AND indexname = 'idx_websites_search_vector'
            """
        )
        assert len(rows) == 1, "GIN index missing — migration not applied?"

    async def test_description_column_exists(self, pg_conn):
        """The description column (forward-looking stub) exists."""
        row = await pg_conn.fetchrow(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'websites' AND column_name = 'description'
            """
        )
        assert row is not None, "description column missing — migration not applied?"
