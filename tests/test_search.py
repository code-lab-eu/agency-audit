"""Tests for agency_audit.search — full-text search with tsvector/tsquery.

Mock-based tests that verify the SQL, parameter passing, and edge cases.
PostgreSQL-backed integration tests are in test_search_integration.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agency_audit.search import (
    _MAX_SEARCH_LIMIT,
    _MIN_SEARCH_LIMIT,
    search_agencies,
    set_agency_description,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _make_pool_mock(*, fetch_return=None, execute_return=None):
    """Create a pool mock that returns configurable rows from conn.fetch().

    Returns (mock_pool, mock_conn) so callers can inspect mock_conn
    after the call if needed.
    """
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=fetch_return or [])
    mock_conn.execute = AsyncMock(return_value=execute_return)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_ctx

    return mock_pool, mock_conn


def _make_row(
    id_=1,
    url="https://example.com",
    label="Test Agency",
    description: str | None = "A great real estate agency",
    score=85,
    audit_status="audited",
    rank=0.9,
):
    """Create a dict-like asyncpg.Record stand-in."""
    return {
        "id": id_,
        "url": url,
        "label": label,
        "description": description,
        "score": score,
        "audit_status": audit_status,
        "rank": rank,
    }


# ── tests ───────────────────────────────────────────────────────────────────


class TestSearchAgencies:
    """Core search_agencies() behaviour."""

    @pytest.mark.asyncio
    async def test_returns_matching_agencies(self):
        """Basic happy path: query returns rows ordered by rank DESC."""
        rows = [
            _make_row(id_=1, label="Alpha Agency", rank=0.95),
            _make_row(id_=2, label="Beta Agency", rank=0.72),
        ]
        pool_mock, conn_mock = _make_pool_mock(fetch_return=rows)

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("alpha beta")

        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2
        assert result[0]["rank"] == 0.95

        conn_mock.fetch.assert_awaited_once()
        call_args = conn_mock.fetch.call_args
        assert call_args.args[1] == "alpha beta"  # query string
        assert call_args.args[2] == 20  # default limit

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty_list(self):
        """Blank or whitespace-only query returns [] without hitting DB."""
        pool_mock, conn_mock = _make_pool_mock()

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("   ")

        assert result == []
        conn_mock.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_list(self):
        """When no rows match, return an empty list."""
        pool_mock, _conn_mock = _make_pool_mock(fetch_return=[])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("nonexistent")

        assert result == []

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self):
        """The caller-supplied (valid) limit is passed through to SQL."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[_make_row()])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("test", limit=5)

        call_args = conn_mock.fetch.call_args
        assert call_args.args[2] == 5

    @pytest.mark.asyncio
    async def test_limit_below_minimum_is_clamped(self):
        """limit < 1 is clamped to _MIN_SEARCH_LIMIT (1)."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[_make_row()])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("test", limit=0)

        call_args = conn_mock.fetch.call_args
        assert call_args.args[2] == _MIN_SEARCH_LIMIT

    @pytest.mark.asyncio
    async def test_limit_negative_is_clamped(self):
        """Negative limit is clamped to _MIN_SEARCH_LIMIT (1)."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[_make_row()])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("test", limit=-10)

        call_args = conn_mock.fetch.call_args
        assert call_args.args[2] == _MIN_SEARCH_LIMIT

    @pytest.mark.asyncio
    async def test_limit_above_maximum_is_clamped(self):
        """limit > _MAX_SEARCH_LIMIT is clamped."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[_make_row()])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("test", limit=9999)

        call_args = conn_mock.fetch.call_args
        assert call_args.args[2] == _MAX_SEARCH_LIMIT

    @pytest.mark.asyncio
    async def test_result_keys_match_expected_schema(self):
        """Every returned dict has the expected keys."""
        row = _make_row()
        pool_mock, _conn_mock = _make_pool_mock(fetch_return=[row])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("test")

        assert len(result) == 1
        expected_keys = {"id", "url", "label", "description", "score", "audit_status", "rank"}
        assert set(result[0].keys()) == expected_keys

    @pytest.mark.asyncio
    async def test_handles_null_description(self):
        """Rows where description IS NULL should survive dict() conversion."""
        row = _make_row(description=None)
        pool_mock, _conn_mock = _make_pool_mock(fetch_return=[row])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("test")

        assert result[0]["description"] is None

    @pytest.mark.asyncio
    async def test_multiple_rows_preserve_rank_ordering(self):
        """Results must be returned in rank DESC order as the DB sends them."""
        rows = [
            _make_row(id_=3, rank=0.99),
            _make_row(id_=1, rank=0.85),
            _make_row(id_=2, rank=0.45),
        ]
        pool_mock, _conn_mock = _make_pool_mock(fetch_return=rows)

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("test")

        assert [r["rank"] for r in result] == [0.99, 0.85, 0.45]

    @pytest.mark.asyncio
    async def test_acquires_and_releases_pool_connection(self):
        """The pool.acquire() context manager is entered and exited."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_conn
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_ctx

        with patch("agency_audit.search.get_pool", return_value=mock_pool):
            await search_agencies("test")

        mock_pool.acquire.assert_called_once()
        mock_ctx.__aenter__.assert_awaited_once()
        mock_ctx.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_query(self):
        """Leading/trailing whitespace is stripped before passing to SQL."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("  agency name  ")

        call_args = conn_mock.fetch.call_args
        assert call_args.args[1] == "agency name"


class TestSearchSqlInjection:
    """Verify that search_agencies() uses parameterised queries — no SQLi risk."""

    @pytest.mark.asyncio
    async def test_query_with_single_quote(self):
        """A query containing a single quote must not cause SQL errors."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            result = await search_agencies("O'Brien agency")

        assert result == []
        conn_mock.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_query_with_sql_keywords(self):
        """SQL keywords in the query string are treated as search terms, not SQL."""
        pool_mock, conn_mock = _make_pool_mock(fetch_return=[])

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await search_agencies("DROP TABLE websites; --")

        call_args = conn_mock.fetch.call_args
        assert "DROP TABLE" in call_args.args[1]
        conn_mock.fetch.assert_awaited_once()


class TestSearchModuleImports:
    """Sanity-checks for module imports and signatures."""

    def test_search_agencies_is_async_function(self):
        """search_agencies must be an async function."""
        import inspect

        assert inspect.iscoroutinefunction(search_agencies)

    def test_search_agencies_default_limit(self):
        """Default limit is 20 (matching _DEFAULT_SEARCH_LIMIT)."""
        import inspect

        sig = inspect.signature(search_agencies)
        assert sig.parameters["limit"].default == 20


class TestSetAgencyDescription:
    """Tests for set_agency_description() helper."""

    @pytest.mark.asyncio
    async def test_updates_description_column(self):
        """set_agency_description issues an UPDATE with the given value."""
        pool_mock, conn_mock = _make_pool_mock()

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await set_agency_description(42, "Top real estate agency in Sofia")

        conn_mock.execute.assert_awaited_once()
        call_args = conn_mock.execute.call_args
        assert call_args.args[0].startswith("UPDATE websites SET description")
        assert call_args.args[1] == "Top real estate agency in Sofia"
        assert call_args.args[2] == 42

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_description(self):
        """Leading/trailing whitespace is stripped before writing."""
        pool_mock, conn_mock = _make_pool_mock()

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await set_agency_description(1, "  Nice agency  ")

        call_args = conn_mock.execute.call_args
        assert call_args.args[1] == "Nice agency"

    @pytest.mark.asyncio
    async def test_empty_description_sets_null(self):
        """An empty/whitespace-only description sets NULL."""
        pool_mock, conn_mock = _make_pool_mock()

        with patch("agency_audit.search.get_pool", return_value=pool_mock):
            await set_agency_description(1, "   ")

        call_args = conn_mock.execute.call_args
        assert call_args.args[1] is None
