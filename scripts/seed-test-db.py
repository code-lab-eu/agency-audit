#!/usr/bin/env python3
"""Populate a database with schema, countries, and sample cities for the tests.

The integration test suite (e.g. tests/test_mcp_server.py) runs against a live,
seeded PostgreSQL database. Use this script to prepare one, locally or in CI:

    uv run scripts/seed-test-db.py     # local development
    python scripts/seed-test-db.py     # CI (project already installed)

It applies all migrations, seeds the countries, and loads a small fixed set of
cities (tests/fixtures/cities.sql) as a stand-in for the Geonames import, which
is too large to run in tests. The target database is read from the
AGENCY_AUDIT_PG_* settings (see agency_audit.config).
"""

import asyncio
from pathlib import Path

import asyncpg

from agency_audit.config import settings
from agency_audit.migrations import run_migrations

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "src" / "agency_audit" / "migrations"
COUNTRIES_SQL = ROOT / "src" / "agency_audit" / "seed" / "countries.sql"
CITIES_SQL = ROOT / "tests" / "fixtures" / "cities.sql"


async def seed() -> None:
    conn = await asyncpg.connect(dsn=settings.dsn)
    try:
        applied = await run_migrations(conn, MIGRATIONS_DIR)
        print(f"Migrations applied: {applied}")

        await conn.execute(COUNTRIES_SQL.read_text(encoding="utf-8"))
        await conn.execute(CITIES_SQL.read_text(encoding="utf-8"))

        countries = await conn.fetchval("SELECT COUNT(*) FROM countries")
        cities = await conn.fetchval("SELECT COUNT(*) FROM cities")
        print(f"Database seeded: {countries} countries, {cities} cities")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed())
