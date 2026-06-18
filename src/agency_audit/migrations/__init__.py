"""Simple SQL migration runner — applies .sql files in order to a fresh DB."""

from pathlib import Path

import asyncpg


async def run_migrations(conn: asyncpg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply all SQL migration files in order."""
    if migrations_dir is None:
        migrations_dir = Path(__file__).parent

    sql_files = sorted(migrations_dir.glob("*.sql"))
    applied: list[str] = []

    for sql_file in sql_files:
        sql = sql_file.read_text(encoding="utf-8")
        await conn.execute(sql)
        applied.append(sql_file.name)

    return applied
