"""Tests for the daily run (``agency_audit.loop.tick``).

Each test runs against a private database from the ``fresh_db`` fixture
(conftest.py), seeded with the canonical countries (active: ES, BE, BG, RS;
discovery ranks 1, 2, 3 and 32) and 20 pending BG cities. The audit and the
Places search are replaced: the audit marks the website ``audited``, the
discovery reports a fixed number of websites.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from agency_audit.config import settings
from agency_audit.discovery import DiscoveryPipeline
from agency_audit.loop.tick import run_tick
from agency_audit.urls import website_key

WEBSITES_FOUND = 7


async def _audited(pool: asyncpg.Pool, website_id: int, url: str) -> str | None:
    """Stand-in for ``audit_and_store_website``: mark the website audited."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE websites SET audit_status = 'audited' WHERE id = $1", website_id)
    return None


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "google_maps_api_key", "test-key")
    monkeypatch.setattr(settings, "audit_concurrency", 3)
    monkeypatch.setattr(settings, "daily_audit_limit", 100)


@pytest.fixture
def audit() -> Iterator[AsyncMock]:
    with patch(
        "agency_audit.loop.tick.audit_and_store_website", AsyncMock(side_effect=_audited)
    ) as mock:
        yield mock


@pytest.fixture
def discover() -> Iterator[AsyncMock]:
    with patch.object(
        DiscoveryPipeline, "discover_city", AsyncMock(return_value=WEBSITES_FOUND)
    ) as mock:
        yield mock


async def _add_city(
    conn: asyncpg.Connection, country: str, slug: str, population: int, status: str = "pending"
) -> int:
    city_id: int = await conn.fetchval(
        "INSERT INTO cities (country, label, slug, population, discovery_status) "
        "VALUES ($1, $2, $2, $3, $4) RETURNING id",
        country,
        slug,
        population,
        status,
    )
    return city_id


async def _set_status(conn: asyncpg.Connection, country: str, slugs: list[str], status: str):
    await conn.execute(
        "UPDATE cities SET discovery_status = $3 WHERE country = $1 AND slug = ANY($2)",
        country,
        slugs,
        status,
    )


BG_TOP_5 = ["sofia", "plovdiv", "varna", "burgas", "ruse"]


async def _add_website(
    conn: asyncpg.Connection,
    name: str,
    country: str = "BG",
    audit_status: str = "pending",
    claimed_minutes_ago: int | None = None,
) -> int:
    """Insert a website linked to the largest city of *country*; return its id."""
    if not await conn.fetchval("SELECT 1 FROM cities WHERE country = $1", country):
        await _add_city(conn, country, f"{country.lower()}-city", 100_000, "done")
    url = f"https://{name}.example.com/"
    website_id: int = await conn.fetchval(
        "INSERT INTO websites (url, url_key, label, audit_status, audit_claimed_at) "
        "VALUES ($1, $2, $3, $4, now() - make_interval(mins => $5)) RETURNING id",
        url,
        website_key(url),
        name,
        audit_status,
        claimed_minutes_ago,
    )
    await conn.execute(
        "INSERT INTO website_cities (website_id, city_id) "
        "SELECT $1, id FROM cities WHERE country = $2 ORDER BY population DESC LIMIT 1",
        website_id,
        country,
    )
    return website_id


async def _audit_status(conn: asyncpg.Connection, website_id: int) -> str:
    status: str = await conn.fetchval("SELECT audit_status FROM websites WHERE id = $1", website_id)
    return status


def _discovered_city(discover: AsyncMock) -> str:
    discover.assert_awaited_once()
    label: str = discover.call_args.kwargs["city_label"]
    return label


class TestAudit:
    async def test_audit_limit_is_respected(
        self,
        fresh_db: asyncpg.Connection,
        audit: AsyncMock,
        discover: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "daily_audit_limit", 4)
        for n in range(10):
            await _add_website(fresh_db, f"site{n}")

        result = await run_tick()

        assert audit.await_count == 4
        assert result.audited == 4
        assert (
            await fresh_db.fetchval("SELECT count(*) FROM websites WHERE audit_status = 'pending'")
            == 6
        )

    async def test_audits_follow_discovery_rank(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        rs = await _add_website(fresh_db, "rs", country="RS")
        bg = await _add_website(fresh_db, "bg", country="BG")
        es = await _add_website(fresh_db, "es", country="ES")

        await run_tick(audit_limit=2)

        assert await _audit_status(fresh_db, es) == "audited"
        assert await _audit_status(fresh_db, bg) == "audited"
        assert await _audit_status(fresh_db, rs) == "pending"

    async def test_failed_audits_are_counted(
        self, fresh_db: asyncpg.Connection, discover: AsyncMock
    ) -> None:
        await _add_website(fresh_db, "ok")
        await _add_website(fresh_db, "broken")

        async def _audit(pool: asyncpg.Pool, website_id: int, url: str) -> str | None:
            if "broken" in url:
                await pool.execute(
                    "UPDATE websites SET audit_status = 'failed' WHERE id = $1", website_id
                )
                return "boom"
            return await _audited(pool, website_id, url)

        with patch("agency_audit.loop.tick.audit_and_store_website", _audit):
            result = await run_tick()

        assert (result.audited, result.failed) == (1, 1)

    async def test_stale_claims_are_released_and_audited(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        stale = await _add_website(
            fresh_db, "stale", audit_status="auditing", claimed_minutes_ago=600
        )

        result = await run_tick()

        assert result.released_websites == 1
        assert await _audit_status(fresh_db, stale) == "audited"

    async def test_locked_countries_are_never_audited(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        locked = await _add_website(fresh_db, "de", country="DE")

        await run_tick()

        audit.assert_not_awaited()
        assert await _audit_status(fresh_db, locked) == "pending"


class TestDiscovery:
    async def test_no_discovery_while_audits_are_pending(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        for n in range(3):
            await _add_website(fresh_db, f"site{n}")

        result = await run_tick(audit_limit=2)

        discover.assert_not_awaited()
        assert result.city is None

    async def test_discovery_runs_when_no_audit_is_pending(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _add_website(fresh_db, "site")

        result = await run_tick()

        assert _discovered_city(discover) == "Sofia"
        assert (result.city, result.country) == ("Sofia", "BG")
        assert result.websites_found == WEBSITES_FOUND

    async def test_pending_websites_of_locked_countries_do_not_block_discovery(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _add_website(fresh_db, "de", country="DE")

        result = await run_tick()

        assert result.city == "Sofia"

    async def test_lowest_ranked_country_comes_first(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _add_city(fresh_db, "RS", "belgrade", 1_200_000)
        await _add_city(fresh_db, "BE", "liege", 190_000)

        result = await run_tick()

        assert (result.city, result.country) == ("liege", "BE")

    async def test_largest_pending_city_comes_first(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _set_status(fresh_db, "BG", ["sofia", "varna"], "done")

        result = await run_tick()

        assert result.city == "Plovdiv"

    async def test_country_with_its_5_largest_cities_done_is_skipped(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        for n in range(5):
            await _add_city(fresh_db, "ES", f"es-done-{n}", 1_000_000 - n, "done")
        await _add_city(fresh_db, "ES", "es-sixth", 500_000)

        result = await run_tick()

        assert result.city == "Sofia"

    async def test_failed_city_is_not_replaced_by_the_sixth_largest(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        for n in range(4):
            await _add_city(fresh_db, "ES", f"es-done-{n}", 1_000_000 - n, "done")
        await _add_city(fresh_db, "ES", "es-failed", 900_000, "failed")
        await _add_city(fresh_db, "ES", "es-sixth", 500_000)

        result = await run_tick()

        assert result.city == "Sofia"

    async def test_locked_countries_are_never_discovered(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _add_city(fresh_db, "DE", "berlin", 3_600_000)
        await _set_status(fresh_db, "BG", BG_TOP_5, "done")

        result = await run_tick()

        discover.assert_not_awaited()
        assert result.city is None
        assert (
            await fresh_db.fetchval("SELECT discovery_status FROM cities WHERE slug = 'berlin'")
            == "pending"
        )

    async def test_nothing_is_discovered_when_all_active_countries_are_complete(
        self,
        fresh_db: asyncpg.Connection,
        audit: AsyncMock,
        discover: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _set_status(fresh_db, "BG", BG_TOP_5, "done")

        with caplog.at_level(logging.INFO, logger="agency_audit.loop.tick"):
            result = await run_tick()

        discover.assert_not_awaited()
        assert result.city is None
        assert "All active countries are complete" in caplog.text

    async def test_failed_city_keeps_its_country_incomplete(
        self,
        fresh_db: asyncpg.Connection,
        audit: AsyncMock,
        discover: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _set_status(fresh_db, "BG", BG_TOP_5, "done")
        await _set_status(fresh_db, "BG", ["varna"], "failed")

        with caplog.at_level(logging.INFO, logger="agency_audit.loop.tick"):
            result = await run_tick()

        discover.assert_not_awaited()
        assert result.city is None
        assert "waiting: BG: Varna (failed)" in caplog.text

    async def test_failed_discovery_marks_the_city_failed(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock
    ) -> None:
        with patch.object(
            DiscoveryPipeline, "discover_city", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            result = await run_tick()

        assert (result.city, result.discovery_error) == ("Sofia", "boom")
        row = await fresh_db.fetchrow(
            "SELECT discovery_status, discovery_error FROM cities WHERE slug = 'sofia'"
        )
        assert (row["discovery_status"], row["discovery_error"]) == ("failed", "boom")

    async def test_no_api_key_claims_no_city(
        self,
        fresh_db: asyncpg.Connection,
        audit: AsyncMock,
        discover: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "google_maps_api_key", "")

        result = await run_tick()

        discover.assert_not_awaited()
        assert result.discovery_error is not None
        assert not await fresh_db.fetchval(
            "SELECT count(*) FROM cities WHERE discovery_status <> 'pending'"
        )


class TestLog:
    async def test_summary_is_written_to_audit_log(
        self, fresh_db: asyncpg.Connection, audit: AsyncMock, discover: AsyncMock
    ) -> None:
        await _add_website(fresh_db, "site")

        result = await run_tick()

        row = await fresh_db.fetchrow(
            "SELECT run_type, country, items_processed, items_succeeded, items_failed, summary "
            "FROM audit_log"
        )
        assert row["run_type"] == "tick"
        assert row["country"] == "BG"
        assert (row["items_processed"], row["items_succeeded"], row["items_failed"]) == (2, 2, 0)
        assert result.summary() == (
            f"Tick: 1 audited, 0 failed, discovered Sofia (BG), {WEBSITES_FOUND} websites found"
        )
        assert result.summary() in row["summary"]
