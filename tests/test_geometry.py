"""Tests for the geometry module — spatial queries and location management.

All tests mock the database pool so no live PostGIS is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agency_audit.geometry import bulk_set_locations, query_by_bounding_box, set_location

# ── helpers ────────────────────────────────────────────────────────────────


def _make_conn(fetch_return=None, execute_return=None, executemany_return="UPDATE 3"):
    """Create a mock asyncpg.Connection with transaction support."""
    mock_conn = AsyncMock()
    if fetch_return is not None:
        mock_conn.fetch.return_value = fetch_return
    if execute_return is not None:
        mock_conn.execute.return_value = execute_return
    mock_conn.executemany.return_value = executemany_return
    return mock_conn


def _make_pool(mock_conn):
    """Create a mock asyncpg.Pool whose acquire() returns an async
    context manager that yields *mock_conn*."""
    mock_pool = MagicMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value = mock_ctx
    return mock_pool


def _make_row(**kwargs):
    """Build a dict-like row for asyncpg fetch results."""
    row = MagicMock()
    row.keys.return_value = kwargs.keys()
    row.__getitem__.side_effect = lambda k: kwargs.get(k)
    row.items.return_value = kwargs.items()
    return row


# ── query_by_bounding_box ──────────────────────────────────────────────────


class TestQueryByBoundingBox:
    """Tests for query_by_bounding_box."""

    async def test_returns_websites_in_bounding_box(self):
        """Should return websites whose location falls within the envelope."""
        row1 = _make_row(
            id=1,
            url="https://a.com",
            label="Agency A",
            score=80,
            audit_data={},
            audit_status="audited",
            last_audited_at=None,
            created_at=None,
            location_wkt="POINT(23.3219 42.6977)",
        )
        row2 = _make_row(
            id=2,
            url="https://b.com",
            label="Agency B",
            score=90,
            audit_data={},
            audit_status="audited",
            last_audited_at=None,
            created_at=None,
            location_wkt="POINT(23.3300 42.6900)",
        )
        mock_conn = _make_conn(fetch_return=[row1, row2])
        mock_pool = _make_pool(mock_conn)

        results = await query_by_bounding_box(
            42.0,
            23.0,
            43.0,
            24.0,
            pool=mock_pool,
        )

        assert len(results) == 2
        assert results[0]["id"] == 1
        assert results[0]["url"] == "https://a.com"
        assert results[0]["location_wkt"] == "POINT(23.3219 42.6977)"
        assert results[1]["id"] == 2

        # Verify query parameters
        call_args = mock_conn.fetch.call_args
        assert call_args is not None
        sql, *params = call_args[0]
        assert "ST_MakeEnvelope" in sql
        assert params == [23.0, 42.0, 24.0, 43.0]  # min_lng, min_lat, max_lng, max_lat

    async def test_returns_empty_when_no_matches(self):
        """Should return an empty list when no websites in the bbox."""
        mock_conn = _make_conn(fetch_return=[])
        mock_pool = _make_pool(mock_conn)

        results = await query_by_bounding_box(
            0.0,
            0.0,
            1.0,
            1.0,
            pool=mock_pool,
        )
        assert results == []

    async def test_filters_out_null_locations(self):
        """The query includes 'WHERE location IS NOT NULL' clause."""
        mock_conn = _make_conn(fetch_return=[])
        mock_pool = _make_pool(mock_conn)

        await query_by_bounding_box(
            42.0,
            23.0,
            43.0,
            24.0,
            pool=mock_pool,
        )

        sql = mock_conn.fetch.call_args[0][0]
        assert "location IS NOT NULL" in sql

    async def test_uses_given_connection_directly(self):
        """When conn is provided, pool should NOT be used for acquire/release."""
        mock_conn = _make_conn(fetch_return=[])
        mock_pool = _make_pool(mock_conn)

        results = await query_by_bounding_box(
            42.0,
            23.0,
            43.0,
            24.0,
            conn=mock_conn,
        )
        assert results == []
        # Pool acquire should NOT be called
        mock_pool.acquire.assert_not_called()

    async def test_releases_connection_when_acquired_from_pool(self):
        """When pool is used, the context manager releases automatically."""
        mock_conn = _make_conn(fetch_return=[])
        mock_pool = _make_pool(mock_conn)

        await query_by_bounding_box(
            42.0,
            23.0,
            43.0,
            24.0,
            pool=mock_pool,
        )

        # Acquire was called and the context manager exited (release via __aexit__)
        mock_pool.acquire.assert_called_once()

    async def test_different_bbox_produces_different_params(self):
        """Parameter order should be (min_lng, min_lat, max_lng, max_lat)."""
        mock_conn = _make_conn(fetch_return=[])
        mock_pool = _make_pool(mock_conn)

        await query_by_bounding_box(
            -10.0,
            -20.0,
            30.0,
            40.0,
            pool=mock_pool,
        )

        _, *params = mock_conn.fetch.call_args[0]
        assert params == [-20.0, -10.0, 40.0, 30.0]

    async def test_raises_when_neither_conn_nor_pool(self):
        """Should raise ValueError when neither conn nor pool is provided."""
        with pytest.raises(ValueError, match="Either conn or pool"):
            await query_by_bounding_box(0.0, 0.0, 1.0, 1.0)


# ── set_location ───────────────────────────────────────────────────────────


class TestSetLocation:
    """Tests for set_location."""

    async def test_sets_location_for_website(self):
        """Should call UPDATE with correct spatial function and parameters."""
        mock_conn = _make_conn()
        mock_pool = _make_pool(mock_conn)

        await set_location(42, 51.5074, -0.1278, pool=mock_pool)

        call_args = mock_conn.execute.call_args
        sql, *params = call_args[0]
        assert "SET location = ST_SetSRID(ST_MakePoint" in sql
        assert "WHERE id" in sql
        # params: (lng, lat, website_id)
        assert params == [-0.1278, 51.5074, 42]

    async def test_uses_connection_directly(self):
        """When conn is provided, should not acquire from pool."""
        mock_conn = _make_conn()
        mock_pool = _make_pool(mock_conn)

        await set_location(1, 0.0, 0.0, conn=mock_conn)

        mock_pool.acquire.assert_not_called()

    async def test_releases_connection_from_pool(self):
        """Should release the connection when context exits."""
        mock_conn = _make_conn()
        mock_pool = _make_pool(mock_conn)

        await set_location(1, 0.0, 0.0, pool=mock_pool)

        mock_pool.acquire.assert_called_once()

    async def test_raises_without_conn_or_pool(self):
        """Should raise ValueError with neither conn nor pool."""
        with pytest.raises(ValueError, match="Either conn or pool"):
            await set_location(1, 0.0, 0.0)


# ── bulk_set_locations ─────────────────────────────────────────────────────


class TestBulkSetLocations:
    """Tests for bulk_set_locations."""

    async def test_bulk_updates_multiple_websites(self):
        """Should call executemany with all rows in the correct order."""
        mock_conn = _make_conn(executemany_return="UPDATE 3")
        mock_pool = _make_pool(mock_conn)

        rows = [
            (1, 42.0, 23.0),
            (2, 43.0, 24.0),
            (3, 44.0, 25.0),
        ]
        result = await bulk_set_locations(rows, pool=mock_pool)
        assert result == 3

        call_args = mock_conn.executemany.call_args
        sql, params_list = call_args[0]
        assert "ST_SetSRID(ST_MakePoint" in sql
        # executemany with (lat, lng, website_id) tuples
        assert params_list == [(42.0, 23.0, 1), (43.0, 24.0, 2), (44.0, 25.0, 3)]

    async def test_bulk_returns_zero_for_no_rows(self):
        """Should return 0 when the rows list is empty."""
        mock_conn = _make_conn(executemany_return="UPDATE 0")
        mock_pool = _make_pool(mock_conn)

        result = await bulk_set_locations([], pool=mock_pool)
        assert result == 0

    async def test_bulk_releases_connection(self):
        """Should release connection when context exits."""
        mock_conn = _make_conn()
        mock_pool = _make_pool(mock_conn)

        await bulk_set_locations([(1, 0.0, 0.0)], pool=mock_pool)

        mock_pool.acquire.assert_called_once()

    async def test_bulk_raises_without_conn_or_pool(self):
        """Should raise ValueError with neither conn nor pool."""
        with pytest.raises(ValueError, match="Either conn or pool"):
            await bulk_set_locations([(1, 0.0, 0.0)])
