"""Simple SQL migration runner — applies .sql files in order, skipping already-applied ones."""

import contextlib
from pathlib import Path

import asyncpg
from asyncpg.exceptions import UndefinedTableError


async def run_migrations(conn: asyncpg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply all unapplied SQL migration files in order.

    Skips files already recorded in the ``schema_migrations`` ledger.
    Each migration executes inside a transaction so partial application
    never leaves the schema in a broken state.
    """
    if migrations_dir is None:
        migrations_dir = Path(__file__).parent

    sql_files = sorted(migrations_dir.glob("*.sql"))
    applied: list[str] = []

    for sql_file in sql_files:
        version = sql_file.name

        # Skip already-applied migrations.  On the very first run the
        # schema_migrations table does not exist yet — that is fine,
        # 000_schema_migrations.sql creates it.
        try:
            already_applied = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)",
                version,
            )
            if already_applied:
                continue
        except UndefinedTableError:
            pass  # First-ever run — table not created yet

        sql = sql_file.read_text(encoding="utf-8")

        async with conn.transaction():
            await conn.execute(sql)
            # After 000_schema_migrations.sql runs the table exists;
            # the INSERT succeeds for every migration from that point on.
            with contextlib.suppress(UndefinedTableError):
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)",
                    version,
                )

        applied.append(version)

    return applied
