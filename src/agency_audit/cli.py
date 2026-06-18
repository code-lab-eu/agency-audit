"""Typer CLI for agency-audit."""

import asyncio
from pathlib import Path

import asyncpg
import typer
from rich.console import Console
from rich.table import Table

from agency_audit.config import settings
from agency_audit.db import close_pool, get_pool
from agency_audit.geonames import import_geonames_for_countries
from agency_audit.migrations import run_migrations

console = Console()

app = typer.Typer(
    name="agency-audit",
    help="Real Estate Radar — Website Discovery & Audit System",
    no_args_is_help=True,
)

# --- db subcommands ---------------------------------------------------------


@app.command("db-init")
def db_init():
    """Apply migrations to a fresh database."""

    async def _run():
        conn = await asyncpg.connect(dsn=settings.dsn)
        try:
            migrations_dir = Path(__file__).parent / "migrations"
            applied = await run_migrations(conn, migrations_dir)
            console.print(f"[green]Applied {len(applied)} migrations:[/]")
            for m in applied:
                console.print(f"  - {m}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command("seed-countries")
def seed_countries():
    """Seed the countries table with the 44-country whitelist."""

    async def _run():
        conn = await asyncpg.connect(dsn=settings.dsn)
        try:
            seed_file = Path(__file__).parent / "seed" / "countries.sql"
            sql = seed_file.read_text(encoding="utf-8")
            await conn.execute(sql)
            count = await conn.fetchval("SELECT COUNT(*) FROM countries")
            console.print(f"[green]Seeded {count} countries.[/]")
        finally:
            await conn.close()

    asyncio.run(_run())


# --- geonames subcommands ----------------------------------------------------


@app.command("import-geonames")
def import_geonames_cmd(
    countries: str | None = typer.Option(
        None, "--countries", "-c", help="Comma-separated ISO codes to import (default: all 44)"
    ),
):
    """Import cities from Geonames dump into the database."""
    country_list = countries.split(",") if countries else None

    async def _run():
        conn = await asyncpg.connect(dsn=settings.dsn)
        try:
            results = await import_geonames_for_countries(conn, country_list)
            table = Table(title="Geonames Import Results")
            table.add_column("Country", style="cyan")
            table.add_column("Cities", justify="right", style="green")
            for code, count in sorted(results.items()):
                table.add_row(code, str(count))
            total = sum(results.values())
            table.add_row("[bold]Total[/]", f"[bold]{total}[/]")
            console.print(table)
        finally:
            await conn.close()

    asyncio.run(_run())


# --- stats -------------------------------------------------------------------


@app.command("stats")
def stats():
    """Show database statistics."""

    async def _run():
        pool = await get_pool()
        try:
            countries = await pool.fetchval("SELECT COUNT(*) FROM countries")
            cities = await pool.fetchval("SELECT COUNT(*) FROM cities")
            websites = await pool.fetchval("SELECT COUNT(*) FROM websites")
            audited = await pool.fetchval(
                "SELECT COUNT(*) FROM websites WHERE audit_status = 'audited'"
            )
            pending = await pool.fetchval(
                "SELECT COUNT(*) FROM websites WHERE audit_status = 'pending'"
            )
        finally:
            await close_pool()

        table = Table(title="Agency Audit — Database Stats")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")
        table.add_row("Countries", str(countries))
        table.add_row("Cities", str(cities))
        table.add_row("Websites", str(websites))
        table.add_row("Audited", str(audited))
        table.add_row("Pending", str(pending))
        console.print(table)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
