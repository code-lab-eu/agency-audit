"""Migration 007 merges websites that differ only by tracking parameters.

The test builds a private database at migration 006, stores the website rows
found on /country/BE, applies 007 and checks the merge.  The key the migration
computes in SQL must equal :func:`agency_audit.urls.website_key` for every row.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from agency_audit.migrations import run_migrations
from agency_audit.urls import clean_website_url, website_key

MIGRATIONS = Path(__file__).resolve().parents[1] / "src" / "agency_audit" / "migrations"
MIGRATION_007 = MIGRATIONS / "007_add_website_url_key.sql"

HEYLEN_ANTWERPEN = (
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+antwerpen"
)
HEYLEN_ZAND = (
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+antwerpen+%27t+zand"
)
HEYLEN_HASSELT = (
    "https://www.heylenvastgoed.be/?utm_source=organic&utm_medium=google+my+business"
    "&utm_campaign=gmb+hasselt&utm_id=google"
)
DISTINCT = [
    "https://www.engelvoelkers.com/be/nl/shops/antwerpen-centrum",
    "https://www.engelvoelkers.com",
    "https://www.era.be/fr/era-chatelain?utm_campaign=era-chatelain-gmb&utm_source=gmb",
    "http://www.era.be/stadsgoed",
    "https://maps.google.com/?cid=ChIJoR3C94XDw0cRxbuBuURXuwo",
    "https://maps.google.com/?cid=ChIJS0GbT-QgwUcRj17w7me9PI4",
    "HTTPS://Example.BE:8443/Path/?b=2&a=1&fbclid=x#top",
]


def _with_db(dsn: str, name: str) -> str:
    return urlunparse(urlparse(dsn)._replace(path=f"/{name}"))


@pytest.fixture
async def db_at_006(postgres_dsn: str, tmp_path: Path) -> AsyncIterator[asyncpg.Connection]:
    """A private database with migrations up to 006 applied."""
    for sql in MIGRATIONS.glob("*.sql"):
        if sql.name < "007":
            shutil.copy(sql, tmp_path / sql.name)
    name = f"aa_test_m007_{uuid.uuid4().hex[:8]}"
    maint = await asyncpg.connect(dsn=postgres_dsn)
    await maint.execute(f'CREATE DATABASE "{name}"')
    conn = await asyncpg.connect(dsn=_with_db(postgres_dsn, name))
    try:
        await run_migrations(conn, tmp_path)
        yield conn
    finally:
        await conn.close()
        await maint.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await maint.close()


async def _website(conn: asyncpg.Connection, url: str, **columns: object) -> int:
    names = ", ".join(["url", *columns])
    params = ", ".join(f"${i}" for i in range(1, len(columns) + 2))
    sql = f"INSERT INTO websites ({names}) VALUES ({params}) RETURNING id"
    website_id: int = await conn.fetchval(sql, url, *columns.values())
    return website_id


async def test_migration_merges_tracking_variants_and_keeps_distinct_sites(
    db_at_006: asyncpg.Connection,
) -> None:
    conn = db_at_006
    await conn.execute("INSERT INTO countries (iso, label) VALUES ('BE', 'Belgium')")
    antwerpen = await conn.fetchval(
        "INSERT INTO cities (country, label, slug, population) "
        "VALUES ('BE', 'Antwerpen', 'antwerpen', 500000) RETURNING id"
    )
    hasselt = await conn.fetchval(
        "INSERT INTO cities (country, label, slug, population) "
        "VALUES ('BE', 'Hasselt', 'hasselt', 80000) RETURNING id"
    )

    first = await _website(conn, HEYLEN_ANTWERPEN, label="Heylen Vastgoed")
    zand = await _website(conn, HEYLEN_ZAND, label="Heylen Vastgoed")
    audited = await _website(
        conn,
        HEYLEN_HASSELT,
        label="Heylen Vastgoed",
        score=65,
        audit_status="audited",
    )
    await conn.execute("UPDATE websites SET last_audited_at = now() WHERE id = $1", audited)
    for website_id, city_id in [(first, antwerpen), (zand, antwerpen), (audited, hasselt)]:
        await conn.execute(
            "INSERT INTO website_cities (website_id, city_id) VALUES ($1, $2)",
            website_id,
            city_id,
        )
    await conn.execute(
        "INSERT INTO discovery_log (city_id, website_id, status) VALUES ($1, $2, 'found')",
        antwerpen,
        zand,
    )
    distinct_ids = {url: await _website(conn, url) for url in DISTINCT}

    await conn.execute(MIGRATION_007.read_text(encoding="utf-8"))

    heylen = await conn.fetch("SELECT * FROM websites WHERE url_key = 'heylenvastgoed.be'")
    assert len(heylen) == 1
    assert heylen[0]["id"] == audited
    assert heylen[0]["score"] == 65
    assert heylen[0]["url"] == "https://www.heylenvastgoed.be/"
    cities = await conn.fetch(
        "SELECT city_id FROM website_cities WHERE website_id = $1 ORDER BY city_id", audited
    )
    assert [r["city_id"] for r in cities] == sorted([antwerpen, hasselt])
    assert await conn.fetchval("SELECT website_id FROM discovery_log") == audited

    remaining = {r["id"]: r for r in await conn.fetch("SELECT id, url, url_key FROM websites")}
    assert set(distinct_ids.values()) <= set(remaining)
    assert len(remaining) == len(DISTINCT) + 1
    for original, website_id in distinct_ids.items():
        row = remaining[website_id]
        assert row["url_key"] == website_key(original), original
        assert row["url"] == clean_website_url(original), original


async def test_migration_makes_url_key_the_identity(db_at_006: asyncpg.Connection) -> None:
    conn = db_at_006
    await conn.execute(MIGRATION_007.read_text(encoding="utf-8"))

    await conn.execute("INSERT INTO websites (url, url_key) VALUES ('https://www.x.be/', 'x.be')")
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute("INSERT INTO websites (url, url_key) VALUES ('http://x.be', 'x.be')")
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute("INSERT INTO websites (url) VALUES ('https://y.be')")
