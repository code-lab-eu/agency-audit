"""Retry logic with exponential backoff for failed discoveries and audits.

Uses configurable retry config: 3 attempts, exponential backoff.
After all retries exhausted, marks items as failed for manual inspection.

Usage:
    from agency_audit.loop.retry import retry, mark_failed, RetryConfig

    async def fetch_website(url):
        ...

    result = await retry(fetch_website, url, max_attempts=3)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behaviour."""

    max_attempts: int = 3
    base_delay: float = 2.0  # seconds
    backoff_factor: float = 2.0  # exponential multiplier
    max_delay: float = 60.0  # cap


DEFAULT_RETRY_CONFIG = RetryConfig()


# ──────────────────────────────────────────────────────────────────────
# Core retry wrapper
# ──────────────────────────────────────────────────────────────────────


async def retry[T](
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> T:
    """Execute an async function with retry and exponential backoff.

    Args:
        func: Async callable to retry.
        *args: Positional arguments passed to func.
        max_attempts: Maximum number of attempts (default: 3).
        base_delay: Initial delay in seconds (default: 2s).
        backoff_factor: Multiplier for each subsequent delay (default: 2x).
        max_delay: Maximum delay cap in seconds (default: 60s).
        retryable_exceptions: Tuple of exception types to retry on.
        **kwargs: Keyword arguments passed to func.

    Returns:
        The return value of func on success.

    Raises:
        The last exception after all retries are exhausted.
    """

    for attempt in range(1, max_attempts + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as exc:
            if attempt == max_attempts:
                logger.error(
                    "All %d attempts failed for %s(%r, %r): %s",
                    max_attempts,
                    getattr(func, "__name__", func),
                    args,
                    kwargs,
                    exc,
                )
                raise

            delay = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
            logger.warning(
                "Attempt %d/%d failed for %s: %s — retrying in %.1fs",
                attempt,
                max_attempts,
                getattr(func, "__name__", func),
                exc,
                delay,
            )
            await asyncio.sleep(delay)

    # Unreachable when max_attempts >= 1: the loop always returns or raises.
    raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")


# ──────────────────────────────────────────────────────────────────────
# Failure marking (for items that exhaust all retries)
# ──────────────────────────────────────────────────────────────────────


async def mark_failed_website(website_id: int, error: str) -> None:
    """Mark a website as failed after exhausting retries."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE websites
               SET audit_status = 'failed',
                   audit_last_error = $1,
                   audit_attempts = audit_attempts + 1
               WHERE id = $2""",
            error,
            website_id,
        )
        # Also log in audit_log
        await conn.execute(
            """INSERT INTO audit_log
                   (run_type, country, items_processed, items_failed, summary, error)
               SELECT 'audit', c.country, 1, 1, '{}'::jsonb, $1
               FROM website_cities wc
               JOIN cities c ON wc.city_id = c.id
               WHERE wc.website_id = $2
               LIMIT 1""",
            error,
            website_id,
        )


async def mark_failed_discovery(city_id: int, error: str) -> None:
    """Mark a city discovery as failed after exhausting retries."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE cities
               SET discovery_status = 'skipped'
               WHERE id = $1""",
            city_id,
        )
        await conn.execute(
            """INSERT INTO discovery_log (city_id, agent, search_query, status, last_error, attempt)
               VALUES ($1, 'loop_orchestrator', 'retry_exhausted', 'failed', $2, 3)""",
            city_id,
            error,
        )


async def mark_failed(item_type: str, item_id: int, error: str) -> None:
    """Mark an item (website or city) as failed after exhausting retries.

    Args:
        item_type: 'website' or 'city'.
        item_id: The website or city ID.
        error: Error message describing the failure.
    """
    if item_type == "website":
        await mark_failed_website(item_id, error)
    elif item_type == "city":
        await mark_failed_discovery(item_id, error)
    else:
        raise ValueError(f"Unknown item_type: {item_type}")
