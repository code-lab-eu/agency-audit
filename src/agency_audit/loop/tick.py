"""The daily run: ``agency-audit tick``.

Each run does a bounded amount of work, so a daily timer keeps the index
growing at a steady pace and Places API spending follows the pace at which
websites are audited:

1. Release stale claims.
2. Audit up to ``AGENCY_AUDIT_DAILY_AUDIT_LIMIT`` pending websites of the
   active (unlocked) countries, in ``discovery_rank`` order.
3. When no pending audit is left, discover one city: the largest pending city
   among the 5 largest cities of the lowest-ranked active country that has
   one. A failed city stays failed until it is rediscovered from the
   dashboard.
4. Log a one-line summary and write it to ``audit_log``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import asyncpg

from agency_audit.claims import claim_next_city, claim_websites, release_stale_claims
from agency_audit.config import settings
from agency_audit.db import get_pool
from agency_audit.discovery import DiscoveryPipeline
from agency_audit.loop.orchestrator import audit_and_store_website
from agency_audit.loop.tracking import log_tick_run

logger = logging.getLogger(__name__)

# The number of largest cities of a country that are discovered.
CITIES_PER_COUNTRY = 5


@dataclass
class TickResult:
    """The outcome of one tick."""

    released_websites: int = 0
    released_cities: int = 0
    audited: int = 0
    failed: int = 0
    city: str | None = None
    country: str | None = None
    websites_found: int = 0
    discovery_error: str | None = None
    duration_seconds: float = 0.0

    def summary(self) -> str:
        """Return the one-line summary of the tick."""
        if self.city is None:
            discovery = self.discovery_error or "no city discovered"
        elif self.discovery_error is not None:
            discovery = f"discovery of {self.city} ({self.country}) failed: {self.discovery_error}"
        else:
            discovery = (
                f"discovered {self.city} ({self.country}), {self.websites_found} websites found"
            )
        return f"Tick: {self.audited} audited, {self.failed} failed, {discovery}"


async def run_tick(audit_limit: int | None = None) -> TickResult:
    """Run one tick.

    Args:
        audit_limit: Maximum number of websites to audit; defaults to
            ``AGENCY_AUDIT_DAILY_AUDIT_LIMIT``.

    Returns:
        The outcome, also written to ``audit_log``.
    """
    started_at = time.monotonic()
    if audit_limit is None:
        audit_limit = settings.daily_audit_limit
    result = TickResult()

    released = await release_stale_claims()
    result.released_websites = released["websites"]
    result.released_cities = released["cities"]

    countries = await _active_countries()
    result.audited, result.failed = await _audit_pending_websites(
        countries, audit_limit, settings.audit_concurrency
    )

    if not await _pending_audits_remain():
        await _discover_next_city(countries, result)

    result.duration_seconds = round(time.monotonic() - started_at, 2)
    logger.info(result.summary())
    await log_tick_run(result)
    return result


async def _active_countries() -> list[str]:
    """Return the ISO codes of the active countries, lowest discovery rank first."""
    pool = await get_pool()
    rows = await pool.fetch("SELECT iso FROM countries WHERE active ORDER BY discovery_rank")
    return [row["iso"] for row in rows]


async def _audit_pending_websites(
    countries: list[str], limit: int, concurrency: int
) -> tuple[int, int]:
    """Audit up to *limit* pending websites of *countries*, in their order.

    Each of *concurrency* workers claims one website at a time, from the first
    country in *countries* that has a pending website left.

    Returns:
        The number of audits that succeeded and the number that failed.
    """
    pool = await get_pool()
    budget = limit
    country_index = 0
    succeeded = 0
    failed = 0

    async def _claim() -> asyncpg.Record | None:
        nonlocal budget, country_index
        if budget <= 0:
            return None
        budget -= 1
        while country_index < len(countries):
            index = country_index
            claimed = await claim_websites(1, country=countries[index])
            if claimed:
                return claimed[0]
            # Another worker may have moved past this country while we waited.
            country_index = max(country_index, index + 1)
        return None

    async def _worker() -> None:
        nonlocal succeeded, failed
        while website := await _claim():
            error = await audit_and_store_website(pool, website["id"], website["url"])
            if error is None:
                succeeded += 1
            else:
                failed += 1

    await asyncio.gather(*(_worker() for _ in range(concurrency)))
    return succeeded, failed


async def _pending_audits_remain() -> bool:
    """Return whether a website of an active country waits for its audit."""
    pool = await get_pool()
    remain: bool = await pool.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM websites w
            JOIN website_cities wc ON wc.website_id = w.id
            JOIN cities c ON c.id = wc.city_id
            JOIN countries co ON co.iso = c.country
            WHERE w.audit_status = 'pending'
              AND w.audit_attempts < 3
              AND co.active
        )
        """
    )
    return remain


async def _discover_next_city(countries: list[str], result: TickResult) -> None:
    """Discover the next city of *countries* and record the outcome in *result*."""
    pipeline = DiscoveryPipeline()
    try:
        if not pipeline.places.available:
            result.discovery_error = "no Google Maps API key configured"
            logger.error("Discovery skipped: %s", result.discovery_error)
            return

        city = None
        for iso in countries:
            city = await claim_next_city(iso, among_largest=CITIES_PER_COUNTRY)
            if city is not None:
                break
        if city is None:
            await _log_nothing_to_discover()
            return

        result.city = city["label"]
        result.country = city["country"]
        try:
            result.websites_found = await pipeline.discover_claimed_city(city)
        except Exception as exc:
            result.discovery_error = str(exc)
    finally:
        await pipeline.close()


async def _log_nothing_to_discover() -> None:
    """Log why no city of the active countries is left to discover."""
    pool = await get_pool()
    incomplete = await pool.fetch(
        """
        SELECT co.iso, string_agg(c.label || ' (' || c.discovery_status || ')', ', '
                                  ORDER BY c.population DESC) AS cities
        FROM countries co
        CROSS JOIN LATERAL (
            SELECT label, discovery_status, population
            FROM cities
            WHERE country = co.iso
            ORDER BY population DESC, id
            LIMIT $1
        ) c
        WHERE co.active AND c.discovery_status <> 'done'
        GROUP BY co.iso, co.discovery_rank
        ORDER BY co.discovery_rank
        """,
        CITIES_PER_COUNTRY,
    )
    if not incomplete:
        logger.info("All active countries are complete; unlock the next country in the dashboard")
        return
    logger.warning(
        "No pending city among the %d largest cities of the active countries; waiting: %s",
        CITIES_PER_COUNTRY,
        "; ".join(f"{row['iso']}: {row['cities']}" for row in incomplete),
    )
