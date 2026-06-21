"""Minimal DB setup for CI — runs migrations and seeds test data.

This script is called from GitHub Actions with a fresh PostgreSQL service
container. It applies the schema, seeds countries, and inserts enough
Bulgarian cities for integration tests to pass.
"""

import asyncio
from pathlib import Path

import asyncpg

from agency_audit.config import settings
from agency_audit.migrations import run_migrations

# Minimal Bulgarian city seed data for integration tests
BG_CITIES_SQL = """
INSERT INTO cities (country, label, slug, population, latitude, longitude) VALUES
    ('BG', 'Sofia', 'sofia', 1286383, 42.6977, 23.3219),
    ('BG', 'Plovdiv', 'plovdiv', 346893, 42.1354, 24.7453),
    ('BG', 'Varna', 'varna', 334870, 43.2141, 27.9147),
    ('BG', 'Burgas', 'burgas', 200271, 42.5048, 27.4626),
    ('BG', 'Ruse', 'ruse', 149642, 43.8560, 25.9785),
    ('BG', 'Stara Zagora', 'stara-zagora', 138272, 42.4258, 25.6345),
    ('BG', 'Pleven', 'pleven', 106954, 43.4170, 24.6063),
    ('BG', 'Dobrich', 'dobrich', 91030, 43.5726, 27.8273),
    ('BG', 'Sliven', 'sliven', 91470, 42.6814, 26.3287),
    ('BG', 'Shumen', 'shumen', 80855, 43.2714, 26.9228),
    ('BG', 'Pernik', 'pernik', 80191, 42.6052, 23.0339),
    ('BG', 'Yambol', 'yambol', 74132, 42.4842, 26.5035),
    ('BG', 'Haskovo', 'haskovo', 76397, 41.9344, 25.5550),
    ('BG', 'Pazardzhik', 'pazardzhik', 75846, 42.1928, 24.3336),
    ('BG', 'Blagoevgrad', 'blagoevgrad', 70881, 42.0219, 23.0963),
    ('BG', 'Veliko Tarnovo', 'veliko-tarnovo', 68783, 43.0757, 25.6172),
    ('BG', 'Vratsa', 'vratsa', 60682, 43.2102, 23.5622),
    ('BG', 'Gabrovo', 'gabrovo', 58950, 42.8747, 25.3342),
    ('BG', 'Vidin', 'vidin', 48071, 43.9900, 22.8725),
    ('BG', 'Kardzhali', 'kardzhali', 43880, 41.6338, 25.3777)
ON CONFLICT (country, slug) DO NOTHING;
"""


async def setup_ci_db() -> None:
    """Create schema, seed countries and cities for CI test runs."""
    conn = await asyncpg.connect(dsn=settings.dsn)

    try:
        # Run all migrations
        migrations_path = Path(__file__).parents[2] / "src" / "agency_audit" / "migrations"
        applied = await run_migrations(conn, migrations_path)
        print(f"Migrations applied: {applied}")

        # Seed countries
        seed_path = (
            Path(__file__).parents[2] / "src" / "agency_audit" / "seed" / "countries.sql"
        )
        countries_sql = seed_path.read_text(encoding="utf-8")
        await conn.execute(countries_sql)
        print("Countries seeded")

        # Seed minimal Bulgarian cities
        await conn.execute(BG_CITIES_SQL)
        print("Cities seeded")

        # Verify
        city_count = await conn.fetchval(
            "SELECT COUNT(*) FROM cities WHERE country = 'BG'"
        )
        country_count = await conn.fetchval("SELECT COUNT(*) FROM countries")
        print(f"Verification: {country_count} countries, {city_count} BG cities")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(setup_ci_db())
