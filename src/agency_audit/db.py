"""Asyncpg database connection pool management."""

import asyncpg
from asyncpg.pool import Pool

from agency_audit.config import settings

_pool: Pool | None = None
_pool_closed: bool = False


async def get_pool() -> Pool:
    """Get or create the shared connection pool."""
    global _pool, _pool_closed
    if _pool is None or _pool_closed:
        _pool = await asyncpg.create_pool(
            dsn=settings.dsn,
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
            command_timeout=settings.pg_pool_command_timeout,
        )
        _pool_closed = False
    return _pool


async def close_pool() -> None:
    """Close the shared connection pool."""
    global _pool, _pool_closed
    if _pool and not _pool_closed:
        await _pool.close()
    _pool = None
    _pool_closed = True
