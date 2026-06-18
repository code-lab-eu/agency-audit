"""Asyncpg database connection pool management."""

import asyncpg
from asyncpg.pool import Pool

from agency_audit.config import settings

_pool: Pool | None = None


async def get_pool() -> Pool:
    """Get or create the shared connection pool."""
    global _pool
    if _pool is None or _pool._closed:
        _pool = await asyncpg.create_pool(
            dsn=settings.dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    """Close the shared connection pool."""
    global _pool
    if _pool and not _pool._closed:
        await _pool.close()
    _pool = None
