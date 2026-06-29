"""Tests for the database connection pool management (db.py)."""

from unittest.mock import AsyncMock, patch

import pytest


class TestPoolLifecycle:
    """Tests for pool get/close/re-get lifecycle without any private attributes."""

    @pytest.fixture(autouse=True)
    async def _save_restore_module_state(self):
        """Save and restore module-level pool state so mocks don't leak to other tests."""
        from agency_audit import db

        saved_pool = db._pool
        saved_closed = db._pool_closed
        yield
        db._pool = saved_pool
        db._pool_closed = saved_closed

    @pytest.mark.asyncio
    async def test_get_pool_creates_new_pool(self):
        """get_pool() should create a pool via asyncpg.create_pool on first call."""
        from agency_audit import db

        # Reset module state to ensure clean test
        db._pool = None
        db._pool_closed = False

        mock_pool = AsyncMock()
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_pool

            result = await db.get_pool()

        assert result is mock_pool
        mock_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pool_returns_existing_pool(self):
        """get_pool() should return the existing pool without recreating it."""
        from agency_audit import db

        mock_pool = AsyncMock()
        db._pool = mock_pool
        db._pool_closed = False

        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            result = await db.get_pool()

        assert result is mock_pool
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_pool_calls_close_and_sets_flag(self):
        """close_pool() should close the pool and set _pool_closed = True."""
        from agency_audit import db

        mock_pool = AsyncMock()
        db._pool = mock_pool
        db._pool_closed = False

        await db.close_pool()

        mock_pool.close.assert_awaited_once()
        assert db._pool is None
        assert db._pool_closed is True

    @pytest.mark.asyncio
    async def test_close_pool_idempotent_when_already_closed(self):
        """close_pool() should be a no-op when the pool is already closed."""
        from agency_audit import db

        mock_pool = AsyncMock()
        db._pool = mock_pool
        db._pool_closed = True  # Already closed

        await db.close_pool()

        # close() should NOT be called again
        mock_pool.close.assert_not_awaited()
        assert db._pool is None
        assert db._pool_closed is True

    @pytest.mark.asyncio
    async def test_close_pool_idempotent_when_none(self):
        """close_pool() should handle _pool=None gracefully."""
        from agency_audit import db

        db._pool = None
        db._pool_closed = False

        await db.close_pool()

        assert db._pool is None
        # close_pool() signals closed state; next get_pool() will recreate
        assert db._pool_closed is True

    @pytest.mark.asyncio
    async def test_get_close_reget_lifecycle(self):
        """get → close → get lifecycle should recreate the pool properly."""
        from agency_audit import db

        # Reset
        db._pool = None
        db._pool_closed = False

        # First get: create pool
        pool1 = AsyncMock()
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = pool1
            result1 = await db.get_pool()

        assert result1 is pool1
        assert db._pool_closed is False

        # Close
        await db.close_pool()
        assert db._pool is None
        assert db._pool_closed is True
        pool1.close.assert_awaited_once()

        # Re-get: should recreate pool
        pool2 = AsyncMock()
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create2:
            mock_create2.return_value = pool2
            result2 = await db.get_pool()

        assert result2 is pool2
        assert result2 is not pool1  # New pool, not the old one
        assert db._pool_closed is False

    @pytest.mark.asyncio
    async def test_no_private_closed_attribute(self):
        """No reference to _pool._closed should exist in db.py source."""
        from pathlib import Path

        import agency_audit.db as db_module

        source = Path(db_module.__file__).read_text()
        assert "_pool._closed" not in source, (
            "db.py must not reference the private _closed attribute of asyncpg.Pool"
        )
