"""Tests for row claims (``agency_audit.claims``).

Each test runs against a private database from the ``fresh_db`` fixture
(conftest.py), seeded with the canonical countries and 20 BG cities. A claim
of a row that another transaction holds locked must skip that row rather than
wait for it; the tests run such claims under a timeout, so a claim that waits
fails the test instead of hanging it.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from agency_audit.claims import claim_next_city, claim_websites, release_stale_claims
from agency_audit.config import settings
from agency_audit.urls import website_key

CLAIM_TIMEOUT_SECONDS = 10


async def _seed_website(
    conn: asyncpg.Connection,
    name: str,
    *,
    country: str = "BG",
    audit_status: str = "pending",
    claimed_minutes_ago: int | None = None,
) -> int:
    """Insert a website linked to the largest city of *country*; return its id."""
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


async def _website(conn: asyncpg.Connection, website_id: int) -> asyncpg.Record:
    row = await conn.fetchrow(
        "SELECT audit_status, audit_claimed_at FROM websites WHERE id = $1", website_id
    )
    assert row is not None
    return row


async def _city_id(conn: asyncpg.Connection, slug: str) -> int:
    city_id: int = await conn.fetchval(
        "SELECT id FROM cities WHERE country = 'BG' AND slug = $1", slug
    )
    return city_id


class TestClaimWebsites:
    async def test_claim_marks_rows_auditing(self, fresh_db: asyncpg.Connection) -> None:
        website_id = await _seed_website(fresh_db, "one")

        claimed = await claim_websites(5)

        assert [row["id"] for row in claimed] == [website_id]
        row = await _website(fresh_db, website_id)
        assert row["audit_status"] == "auditing"
        assert row["audit_claimed_at"] is not None

    async def test_claim_skips_rows_locked_by_another_claim(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        locked_id = await _seed_website(fresh_db, "locked")
        free_id = await _seed_website(fresh_db, "free")

        async with fresh_db.transaction():
            await fresh_db.execute("SELECT id FROM websites WHERE id = $1 FOR UPDATE", locked_id)
            async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
                claimed = await claim_websites(5)

        assert [row["id"] for row in claimed] == [free_id]
        assert (await _website(fresh_db, locked_id))["audit_status"] == "pending"

    async def test_concurrent_claims_never_return_the_same_row(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        website_ids = {await _seed_website(fresh_db, f"site{i}") for i in range(6)}

        async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
            batches = await asyncio.gather(*(claim_websites(2) for _ in range(4)))

        claimed_ids = [row["id"] for batch in batches for row in batch]
        assert len(claimed_ids) == len(set(claimed_ids))
        assert set(claimed_ids) == website_ids

    async def test_claim_respects_limit(self, fresh_db: asyncpg.Connection) -> None:
        for i in range(3):
            await _seed_website(fresh_db, f"site{i}")

        assert len(await claim_websites(2)) == 2

    async def test_claim_filters_by_country(self, fresh_db: asyncpg.Connection) -> None:
        await fresh_db.execute(
            "INSERT INTO cities (country, label, slug, population) "
            "VALUES ('RS', 'Belgrade', 'belgrade', 1197714)"
        )
        bg_id = await _seed_website(fresh_db, "bg")
        await _seed_website(fresh_db, "rs", country="RS")

        claimed = await claim_websites(5, country="bg")

        assert [row["id"] for row in claimed] == [bg_id]

    async def test_claim_ignores_rows_that_are_not_pending(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        await _seed_website(fresh_db, "audited", audit_status="audited")
        await _seed_website(fresh_db, "auditing", audit_status="auditing", claimed_minutes_ago=1)

        assert await claim_websites(5) == []


class TestClaimNextCity:
    async def test_claim_marks_largest_city_in_progress(self, fresh_db: asyncpg.Connection) -> None:
        city = await claim_next_city("bg")

        assert city is not None
        assert city["slug"] == "sofia"
        row = await fresh_db.fetchrow(
            "SELECT discovery_status, discovery_claimed_at FROM cities WHERE id = $1", city["id"]
        )
        assert row is not None
        assert row["discovery_status"] == "in_progress"
        assert row["discovery_claimed_at"] is not None

    async def test_claim_skips_city_locked_by_another_claim(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        sofia_id = await _city_id(fresh_db, "sofia")

        async with fresh_db.transaction():
            await fresh_db.execute("SELECT id FROM cities WHERE id = $1 FOR UPDATE", sofia_id)
            async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
                city = await claim_next_city("BG")

        assert city is not None
        assert city["slug"] == "plovdiv"

    async def test_concurrent_claims_never_return_the_same_city(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        async with asyncio.timeout(CLAIM_TIMEOUT_SECONDS):
            cities = await asyncio.gather(*(claim_next_city("BG") for _ in range(5)))

        slugs = [city["slug"] for city in cities if city is not None]
        assert sorted(slugs) == sorted(["sofia", "plovdiv", "varna", "burgas", "ruse"])

    async def test_no_pending_city_returns_none(self, fresh_db: asyncpg.Connection) -> None:
        await fresh_db.execute("UPDATE cities SET discovery_status = 'done'")

        assert await claim_next_city() is None

    async def test_claim_among_largest_skips_smaller_cities(
        self, fresh_db: asyncpg.Connection
    ) -> None:
        await fresh_db.execute(
            "UPDATE cities SET discovery_status = 'done' "
            "WHERE slug IN ('sofia', 'plovdiv', 'burgas', 'ruse')"
        )
        await fresh_db.execute("UPDATE cities SET discovery_status = 'failed' WHERE slug = 'varna'")

        assert await claim_next_city("BG", among_largest=5) is None
        city = await claim_next_city("BG", among_largest=6)
        assert city is not None
        assert city["slug"] == "stara-zagora"


class TestReleaseStaleClaims:
    @pytest.fixture
    async def claims(self, fresh_db: asyncpg.Connection) -> dict[str, int]:
        """One stale (3 hours) and one fresh (10 minutes) claim per table."""
        stale_city = await _city_id(fresh_db, "sofia")
        fresh_city = await _city_id(fresh_db, "plovdiv")
        for city_id, minutes_ago in ((stale_city, 180), (fresh_city, 10)):
            await fresh_db.execute(
                "UPDATE cities SET discovery_status = 'in_progress', "
                "discovery_claimed_at = now() - make_interval(mins => $2) WHERE id = $1",
                city_id,
                minutes_ago,
            )
        return {
            "stale_website": await _seed_website(
                fresh_db, "stale", audit_status="auditing", claimed_minutes_ago=180
            ),
            "fresh_website": await _seed_website(
                fresh_db, "fresh", audit_status="auditing", claimed_minutes_ago=10
            ),
            "stale_city": stale_city,
            "fresh_city": fresh_city,
        }

    async def _city_status(self, conn: asyncpg.Connection, city_id: int) -> str:
        status: str = await conn.fetchval(
            "SELECT discovery_status FROM cities WHERE id = $1", city_id
        )
        return status

    async def test_releases_only_claims_older_than_the_limit(
        self, fresh_db: asyncpg.Connection, claims: dict[str, int]
    ) -> None:
        released = await release_stale_claims(120)

        assert released == {"websites": 1, "cities": 1}
        assert (await _website(fresh_db, claims["stale_website"]))["audit_status"] == "pending"
        assert (await _website(fresh_db, claims["fresh_website"]))["audit_status"] == "auditing"
        assert await self._city_status(fresh_db, claims["stale_city"]) == "pending"
        assert await self._city_status(fresh_db, claims["fresh_city"]) == "in_progress"

    async def test_limit_defaults_to_setting(
        self,
        fresh_db: asyncpg.Connection,
        claims: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "claim_timeout_minutes", 5)

        assert await release_stale_claims() == {"websites": 2, "cities": 2}

    async def test_released_website_can_be_claimed_again(
        self, fresh_db: asyncpg.Connection, claims: dict[str, int]
    ) -> None:
        await release_stale_claims(120)

        claimed = await claim_websites(5)

        assert [row["id"] for row in claimed] == [claims["stale_website"]]
