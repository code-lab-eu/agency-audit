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


# --- geonames / import-cities -----------------------------------------------


@app.command("import-cities")
def import_cities(
    country: str = typer.Option(
        None, "--country", "-c", help="ISO country code (e.g. BG). Default: all 44 countries."
    ),
):
    """Import cities from Geonames dump into the database."""
    country_list = [country.upper()] if country else None

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


@app.command("import-geonames")
def import_geonames_cmd(
    countries: str | None = typer.Option(
        None, "--countries", "-c", help="Comma-separated ISO codes to import (default: all 44)"
    ),
):
    """Import cities from Geonames dump (legacy alias for import-cities)."""
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


# --- serve -------------------------------------------------------------------


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Bind address"),
    port: int = typer.Option(8000, "--port", "-p", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload"),
    log_level: str = typer.Option(
        None,
        "--log-level",
        help="Override AGENCY_AUDIT_LOG_LEVEL (default: INFO)",
    ),
):
    """Start the FastAPI + HTMX web dashboard.

    Structured JSON logging is enabled by default (set AGENCY_AUDIT_LOG_FORMAT=console
    for human-readable output). The server handles SIGTERM/SIGINT gracefully: it stops
    accepting new requests, finishes in-flight ones, and closes the database pool.
    """
    import signal

    import uvicorn

    from agency_audit.logging_config import setup_logging

    # Allow CLI flag to override env setting
    if log_level is not None:
        settings.log_level = log_level

    setup_logging()

    console.print(f"[cyan]Starting Agency Audit dashboard on http://{host}:{port} ...[/]")
    if settings.log_format == "json":
        console.print("[dim]Logging in JSON format to stdout[/]")

    # Use uvicorn's programmatic config so we can hook into the server lifecycle
    config = uvicorn.Config(
        "agency_audit.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_config=None,  # we manage logging ourselves
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    # Install signal handlers that tell uvicorn to shut down gracefully.
    # uvicorn will stop accepting new connections, drain in-flight requests,
    # and then exit.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda sig_num, frame: server.handle_exit(sig_num, frame))

    # We need to close the pool AFTER uvicorn's shutdown lifecycle completes.
    # The simplest way: run uvicorn, then close the pool.
    try:
        server.run()
    finally:
        import asyncio

        async def _close_pool():
            from agency_audit.db import close_pool as _cp

            await _cp()

        asyncio.run(_close_pool())
        console.print("[dim]Database pool closed[/]")


# --- discover ----------------------------------------------------------------


# --- audit -------------------------------------------------------------------


@app.command("audit")
def audit(
    website_id: int = typer.Option(
        None, "--website-id", "-w", help="Website ID to audit (uses full pipeline)"
    ),
    url: str = typer.Option(None, "--url", "-u", help="URL to audit directly (uses full pipeline)"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table, json, db"),
):
    """Run a full audit on a website using the complete audit pipeline.

    Uses all 7 audit modules: robots.txt, anti-scraping, API detection,
    property count, listing quality, tech stack, and scoring.

    With --output db: stores results in the database (requires --website-id).
    With --output table (default): prints results as a table.
    With --output json: prints results as JSON.
    """
    if website_id is None and url is None:
        console.print("[red]Either --website-id or --url is required.[/]")
        raise typer.Exit(1)

    async def _run():
        import json

        from agency_audit.audit.auditor import audit_website

        target_url = url
        wid = website_id

        # Resolve URL from DB if website_id provided
        if wid is not None:
            pool = await get_pool()
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT id, url, label FROM websites WHERE id = $1", wid
                    )
                    if row is None:
                        console.print(f"[red]Website {wid} not found.[/]")
                        raise typer.Exit(1)
                    target_url = row["url"]
                    # Mark as auditing
                    await conn.execute(
                        "UPDATE websites SET audit_status = 'auditing' WHERE id = $1",
                        wid,
                    )
            finally:
                await close_pool()

        console.print(f"[cyan]Running full audit on {target_url} ...[/]")

        result = await audit_website(target_url)

        if output == "table":
            table = Table(title=f"Audit Result — {result.url}")
            table.add_column("Check", style="cyan")
            table.add_column("Result", style="green")

            table.add_row(
                "Robots.txt", "Allowed" if result.robots.allows_scraping else "Disallowed"
            )
            if result.robots.crawl_delay:
                table.add_row("Crawl Delay", f"{result.robots.crawl_delay}s")
            if result.robots.sitemap_urls:
                table.add_row("Sitemaps", str(len(result.robots.sitemap_urls)))

            table.add_row("Anti-Scraping", "Detected" if result.anti_scraping.detected else "None")
            if result.anti_scraping.cloudflare:
                table.add_row("  Cloudflare", "Yes")
            if result.anti_scraping.recaptcha:
                table.add_row("  reCAPTCHA", "Yes")

            table.add_row(
                "API",
                f"{result.api_detection.api_type or 'None'} "
                f"({result.api_detection.api_url or '-'})",
            )
            table.add_row(
                "Property Count",
                f"{result.property_count.count:,} ({result.property_count.source}, "
                f"conf={result.property_count.confidence:.1%})",
            )
            table.add_row(
                "Structured Data", "Yes" if result.listing_quality.has_structured_data else "No"
            )
            table.add_row("Has Prices", "Yes" if result.listing_quality.has_prices else "No")
            table.add_row("Has Locations", "Yes" if result.listing_quality.has_locations else "No")
            table.add_row("Has Images", "Yes" if result.listing_quality.has_images else "No")
            table.add_row("Has Map", "Yes" if result.listing_quality.has_property_map else "No")
            table.add_row("Framework", result.tech_stack.framework or "Unknown")
            table.add_row("CDN", result.tech_stack.cdn or "None")
            table.add_row("Hosting", result.tech_stack.hosting or "Unknown")
            table.add_row(
                "Technologies",
                ", ".join(result.tech_stack.technologies)
                if result.tech_stack.technologies
                else "None",
            )
            table.add_row(
                "Response Time",
                f"{result.response_time_ms}ms" if result.response_time_ms else "N/A",
            )
            table.add_row("SSL", "Valid" if result.ssl_valid else "Invalid")
            table.add_row("Language", result.language or "Unknown")

            # Score breakdown
            table.add_section()
            table.add_row("[bold]OVERALL SCORE[/]", f"[bold]{result.score}[/]")
            for check, points in result.score_breakdown.items():
                color = "green" if points > 0 else "red" if points < 0 else "dim"
                table.add_row(f"  {check}", f"[{color}]{points:+d}[/]")

            console.print(table)

        elif output == "json":
            console.print_json(data=json.dumps(result.to_dict()))

        elif output == "db" and wid is not None:
            pool = await get_pool()
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE websites
                        SET audit_data = $1::jsonb,
                            score = $2,
                            audit_status = 'audited',
                            last_audited_at = now()
                        WHERE id = $3
                        """,
                        json.dumps(result.to_dict()),
                        result.score,
                        wid,
                    )
                console.print(f"[green]Audit complete. Score: {result.score} — stored in DB.[/]")
            finally:
                await close_pool()
        else:
            console.print(f"[green]Audit complete. Score: {result.score}[/]")

    asyncio.run(_run())


@app.command("batch-audit")
def batch_audit(
    urls: str = typer.Option(..., "--urls", "-u", help="Comma-separated URLs to audit"),
    concurrency: int = typer.Option(3, "--concurrency", "-c", help="Max concurrent audits"),
    output: str = typer.Option("table", "--output", "-o", help="Output format: table, json"),
):
    """Audit multiple websites concurrently using the full audit pipeline.

    Example: agency-audit batch-audit --urls "https://example.com,https://example.org"
    """
    url_list = [u.strip() for u in urls.split(",") if u.strip()]

    async def _run():
        import json

        from agency_audit.audit.auditor import audit_websites

        console.print(f"[cyan]Auditing {len(url_list)} websites (concurrency={concurrency})...[/]")
        results = await audit_websites(url_list, concurrency=concurrency)

        if output == "json":
            combined = []
            for r in results:
                combined.append(r.to_dict())
            console.print_json(data=json.dumps(combined))
            return

        # Table output
        table = Table(title=f"Batch Audit — {len(results)} websites")
        table.add_column("URL", style="cyan", max_width=40)
        table.add_column("Score", justify="right")
        table.add_column("Robots")
        table.add_column("API")
        table.add_column("Properties", justify="right")
        table.add_column("Framework")
        table.add_column("SSL")
        table.add_column("Response", justify="right")

        for r in results:
            score_color = "green" if r.score >= 50 else "yellow" if r.score >= 0 else "red"
            robots = "✓" if r.robots.allows_scraping else "✗"
            api = r.api_detection.api_type or "-"
            props = str(r.property_count.count) if r.property_count.count > 0 else "-"
            framework = r.tech_stack.framework or "-"
            ssl = "✓" if r.ssl_valid else "✗"
            resp = f"{r.response_time_ms}ms" if r.response_time_ms else "-"

            table.add_row(
                r.url,
                f"[{score_color}]{r.score}[/]",
                robots,
                api,
                props,
                framework,
                ssl,
                resp,
            )

        console.print(table)

        # Summary
        scores = [r.score for r in results]
        avg = sum(scores) / len(scores) if scores else 0
        positive = sum(1 for s in scores if s > 0)
        console.print(
            f"\n[bold]Summary:[/] Avg score: {avg:.0f} | "
            f"Positive: {positive}/{len(results)} | Range: {min(scores)}..{max(scores)}"
        )

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


# --- discover ----------------------------------------------------------------


@app.command("discover")
def discover(
    country: str | None = typer.Option(
        None,
        "--country",
        "-c",
        help="ISO country code (e.g., 'BG'). Default: all pending",
    ),
    countries: str | None = typer.Option(
        None,
        "--countries",
        help="Comma-separated ISO codes (e.g., 'BG,GB,DE'). Overrides --country.",
    ),
    max_cities: int = typer.Option(
        3,
        "--max-cities",
        "-n",
        help="Max cities to process per country",
    ),
):
    """Discover real estate agencies via the Google Maps Places API.

    Reads AGENCY_AUDIT_GOOGLE_MAPS_API_KEY from the environment (or .env).
    An API key is required; discovery exits with an error if none is set.

    Each city's discovery_status is updated to 'done' when finished.
    Agencies are inserted into the websites table and linked via
    website_cities. All operations are logged in discovery_log.
    """

    async def _run():
        from agency_audit.discovery import run_discovery

        # --countries takes precedence over --country
        if countries:
            country_list = countries.split(",")
        elif country:
            country_list = [country.upper()]
        else:
            country_list = None
        try:
            summary = await run_discovery(countries=country_list, max_cities=max_cities)
        except RuntimeError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1) from None

        table = Table(title="Discovery Pipeline Results")
        table.add_column("Country", style="cyan")
        table.add_column("Cities", justify="right", style="green")
        table.add_column("Agencies", justify="right", style="yellow")
        for code, data in summary.get("results", {}).items():
            table.add_row(code, str(data.get("cities", 0)), str(data.get("agencies", 0)))
        if summary.get("results"):
            total_cities = summary.get("cities_processed", 0)
            total_agencies = summary.get("agencies_found", 0)
            table.add_row(
                "[bold]Total[/]",
                f"[bold]{total_cities}[/]",
                f"[bold]{total_agencies}[/]",
            )
        else:
            console.print("[yellow]No pending cities found for the specified countries.[/]")
        console.print(table)

    asyncio.run(_run())


# --- run (full loop) ---------------------------------------------------------


@app.command("run")
def run_cmd(
    country: str = typer.Option(
        ..., "--country", "-c", help="ISO country code (e.g., 'BG'). Required."
    ),
    max_cities: int | None = typer.Option(
        None, "--max-cities", "-n", help="Max cities to discover (default: all pending)"
    ),
    concurrency: int = typer.Option(3, "--concurrency", help="Max concurrent audits"),
    reaudit: int = typer.Option(
        30, "--reaudit-days", help="Days until re-audit triggers (default: 30)"
    ),
    reaudit_limit: int = typer.Option(
        100, "--reaudit-limit", help="Max websites to queue for re-audit"
    ),
    skip_discovery: bool = typer.Option(False, "--skip-discovery", help="Skip the discovery phase"),
    skip_audit: bool = typer.Option(False, "--skip-audit", help="Skip the audit phase"),
    skip_qc: bool = typer.Option(False, "--skip-qc", help="Skip QC checks"),
    skip_reaudit: bool = typer.Option(False, "--skip-reaudit", help="Skip re-audit scheduling"),
):
    """Execute full operational loop for one country: discover → audit → QC → re-audit."""
    from agency_audit.loop.orchestrator import run_country

    async def _run():
        result = await run_country(
            country_iso=country.upper(),
            max_cities=max_cities,
            audit_concurrency=concurrency,
            reaudit_interval_days=reaudit,
            reaudit_limit=reaudit_limit,
            skip_discovery=skip_discovery,
            skip_audit=skip_audit,
            skip_qc=skip_qc,
            skip_reaudit=skip_reaudit,
        )

        # Print summary table
        table = Table(title=f"Loop Results — {result['country']}")
        table.add_column("Phase", style="cyan")
        table.add_column("Result", style="green")

        for phase_name in ["discovery", "audit", "qc", "reaudit"]:
            phase = result["phases"].get(phase_name, {})
            if not phase:
                continue
            if "error" in phase:
                table.add_row(phase_name.capitalize(), f"[red]ERROR: {phase['error']}[/]")
            elif phase_name == "discovery":
                table.add_row(
                    "Discovery",
                    f"{phase.get('cities_processed', 0)} cities, "
                    f"{phase.get('agencies_found', 0)} agencies "
                    f"({phase.get('duration_seconds', 0)}s)",
                )
            elif phase_name == "audit":
                # Orchestrator returns audits_succeeded / audits_failed
                audits_ok = phase.get("audits_succeeded", 0)
                audits_fail = phase.get("audits_failed", 0)
                table.add_row(
                    "Audit",
                    f"{audits_ok} ✓ / {audits_fail} ✗ ({phase.get('duration_seconds', 0)}s)",
                )
            elif phase_name == "qc":
                table.add_row(
                    "QC",
                    f"{phase.get('findings', 0)} findings "
                    f"(scores: {phase.get('suspicious_scores', 0)}, "
                    f"dupes: {phase.get('duplicate_domains', 0)})",
                )
            elif phase_name == "reaudit":
                table.add_row(
                    "Re-audit",
                    f"{phase['queued']} websites queued "
                    f"(oldest: {phase.get('oldest_age_days', 'N/A')}d)",
                )

        table.add_row(
            "[bold]Total Duration[/]",
            f"[bold]{result['duration_seconds']}s[/]",
        )

        errors = result.get("errors", [])
        if errors:
            table.add_row("[red]Errors[/]", f"[red]{len(errors)}[/]")
            for err in errors:
                console.print(f"  [red]  {err}[/]")

        console.print(table)

    asyncio.run(_run())


@app.command("run-all")
def run_all_cmd(
    max_cities: int | None = typer.Option(
        None, "--max-cities", "-n", help="Max cities per country (default: all pending)"
    ),
    concurrency: int = typer.Option(3, "--concurrency", help="Max concurrent audits per country"),
    reaudit: int = typer.Option(
        30, "--reaudit-days", help="Days until re-audit triggers (default: 30)"
    ),
    reaudit_limit: int = typer.Option(
        100, "--reaudit-limit", help="Max websites to queue for re-audit per country"
    ),
    countries: str | None = typer.Option(
        None, "--countries", help="Comma-separated ISO codes (default: all active)"
    ),
):
    """Execute full operational loop for all countries sequentially."""
    from agency_audit.loop.orchestrator import run_all_countries

    country_list = countries.split(",") if countries else None

    async def _run():
        result = await run_all_countries(
            max_cities_per_country=max_cities,
            audit_concurrency=concurrency,
            reaudit_interval_days=reaudit,
            reaudit_limit=reaudit_limit,
            countries=country_list,
        )

        totals = result["totals"]

        table = Table(title="Run-All Results")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")
        table.add_row("Countries processed", str(totals["countries_processed"]))
        table.add_row("Cities discovered", str(totals["cities_processed"]))
        table.add_row("Agencies found", str(totals["agencies_found"]))
        table.add_row("Websites audited", str(totals["websites_audited"]))
        table.add_row("Audits succeeded", str(totals["audits_succeeded"]))
        table.add_row("Audits failed", str(totals["audits_failed"]))
        table.add_row("QC findings", str(totals["qc_findings"]))
        table.add_row("Re-audits queued", str(totals["reaudit_queued"]))

        errors = totals.get("errors", [])
        if errors:
            table.add_row("[red]Errors[/]", f"[red]{len(errors)}[/]")

        console.print(table)

        # Per-country breakdown
        if len(result["results"]) > 1:
            detail = Table(title="Per-Country Breakdown")
            detail.add_column("Country", style="cyan")
            detail.add_column("Status", style="green")
            for iso, res in sorted(result["results"].items()):
                if "error" in res:
                    detail.add_row(iso, f"[red]{res['error']}[/]")
                else:
                    disc = res["phases"].get("discovery", {})
                    audit = res["phases"].get("audit", {})
                    detail.add_row(
                        iso,
                        f"{disc.get('cities_processed', 0)}c/{disc.get('agencies_found', 0)}a "
                        f"| {audit.get('succeeded', 0)}✓/{audit.get('failed', 0)}✗ "
                        f"| {res['duration_seconds']}s",
                    )
            console.print(detail)

    asyncio.run(_run())


# --- qc ----------------------------------------------------------------------


@app.command("qc")
def qc_cmd(
    action: str = typer.Option(
        "run", "--action", "-a", help="Action: run, list-review, mark-review"
    ),
    website_id: int | None = typer.Option(
        None, "--website-id", "-w", help="Website ID (for mark-review)"
    ),
    reason: str | None = typer.Option(None, "--reason", "-r", help="Reason (for mark-review)"),
    severity: str = typer.Option("warning", "--severity", "-s", help="Severity: warning or error"),
):
    """Run quality control checks or manage flagged websites."""
    from agency_audit.loop.qc import (
        get_websites_needing_review,
        mark_for_manual_review,
        run_qc_checks,
    )

    async def _run():
        if action == "run":
            result = await run_qc_checks()
            table = Table(title="QC Check Results")
            table.add_column("Check", style="cyan")
            table.add_column("Findings", justify="right", style="green")
            table.add_row("Suspicious scores (0 or 100)", str(result["suspicious_scores"]))
            table.add_row("Duplicate domains", str(result["duplicate_domains"]))
            table.add_row("[bold]Total[/]", f"[bold]{result['total_findings']}[/]")
            console.print(table)

        elif action == "list-review":
            websites = await get_websites_needing_review()
            if not websites:
                console.print("[green]No websites flagged for review.[/]")
                return
            table = Table(title=f"Websites Needing Review ({len(websites)})")
            table.add_column("ID", justify="right")
            table.add_column("URL", style="cyan", max_width=50)
            table.add_column("Score", justify="right")
            table.add_column("Reason", max_width=60)
            for w in websites:
                table.add_row(str(w["id"]), w["url"], str(w["score"]), w["review_reason"] or "-")
            console.print(table)

        elif action == "mark-review":
            if not website_id or not reason:
                console.print("[red]--website-id and --reason are required for mark-review.[/]")
                raise typer.Exit(1)
            await mark_for_manual_review(website_id, reason, severity)
            console.print(f"[green]Flagged website {website_id} for manual review: {reason}[/]")

    asyncio.run(_run())


# --- reaudit -----------------------------------------------------------------


@app.command("reaudit")
def reaudit_cmd(
    action: str = typer.Option("queue", "--action", "-a", help="Action: queue, trigger"),
    interval: int = typer.Option(
        30, "--interval", "-i", help="Days until re-audit triggers (default: 30)"
    ),
    limit: int = typer.Option(100, "--limit", "-l", help="Max websites to queue (default: 100)"),
    country: str | None = typer.Option(
        None, "--country", "-c", help="Country ISO code to filter by"
    ),
):
    """Manage re-audit queue for websites audited >30 days ago."""
    from agency_audit.loop.reaudit import get_reaudit_queue, schedule_reaudits

    async def _run():
        if action == "queue":
            websites = await get_reaudit_queue(
                interval_days=interval, limit=limit, country=country.upper() if country else None
            )
            if not websites:
                console.print("[green]No websites overdue for re-audit.[/]")
                return

            table = Table(title=f"Re-Audit Queue — {len(websites)} websites (>={interval}d)")
            table.add_column("ID", justify="right")
            table.add_column("URL", style="cyan", max_width=45)
            table.add_column("Score", justify="right")
            table.add_column("Last Audited", max_width=20)
            table.add_column("Age", justify="right")
            table.add_column("Country")

            for w in websites:
                table.add_row(
                    str(w["id"]),
                    w["url"],
                    str(w["score"]),
                    w["last_audited_at"][:10] if w["last_audited_at"] else "never",
                    f"{w['age_days']}d" if w["age_days"] else "-",
                    w["country"] or "-",
                )
            console.print(table)

        elif action == "trigger":
            result = await schedule_reaudits(
                interval_days=interval,
                limit=limit,
                country=country.upper() if country else None,
            )
            console.print(
                f"[green]Re-audit triggered: {result['queued']} websites queued"
                + (
                    f" (oldest: {result['oldest_age_days']}d)"
                    if result.get("oldest_age_days")
                    else ""
                )
                + "[/]"
            )

    asyncio.run(_run())


# --- progress ----------------------------------------------------------------


@app.command("progress")
def progress_cmd():
    """Show overall pipeline progress with per-country stats."""
    from agency_audit.loop.tracking import get_progress

    async def _run():
        data = await get_progress()

        # Overview table
        overview = data["overview"]
        table = Table(title="Agency Audit — Pipeline Progress")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")
        table.add_row("Countries", str(overview["countries"]))
        table.add_row("Cities total", str(overview["cities_total"]))
        table.add_row("Cities done", str(overview["cities_done"]))
        table.add_row("Cities pending", str(overview["cities_pending"]))
        table.add_row("Websites total", str(overview["websites_total"]))
        table.add_row("Websites audited", str(overview["websites_audited"]))
        table.add_row("Websites pending", str(overview["websites_pending"]))
        table.add_row("Websites failed", str(overview["websites_failed"]))
        table.add_row("Needs review", str(overview["websites_needing_review"]))
        table.add_row("Average score", str(overview["avg_score"]))
        console.print(table)

        # Per-country table (top 15 by cities)
        per_country = sorted(data["per_country"], key=lambda x: x["total_cities"], reverse=True)[
            :15
        ]
        if per_country:
            ct = Table(title="Top Countries")
            ct.add_column("Country", style="cyan")
            ct.add_column("Cities", justify="right")
            ct.add_column("Done", justify="right", style="green")
            ct.add_column("Websites", justify="right")
            ct.add_column("Audited", justify="right", style="green")
            ct.add_column("Avg Score", justify="right")
            for c in per_country:
                ct.add_row(
                    c["label"],
                    str(c["total_cities"]),
                    str(c["cities_done"]),
                    str(c["total_websites"]),
                    str(c["websites_audited"]),
                    str(c["avg_score"]),
                )
            console.print(ct)

        # Recent runs
        recent = data["recent_runs"]
        if recent:
            rt = Table(title="Recent Runs (last 20)")
            rt.add_column("ID", justify="right")
            rt.add_column("Type", style="cyan")
            rt.add_column("Country")
            rt.add_column("Success", justify="right", style="green")
            rt.add_column("Failed", justify="right", style="red")
            rt.add_column("Duration", justify="right")
            for r in recent[:10]:
                rt.add_row(
                    str(r["id"]),
                    r["run_type"],
                    r["country"] or "-",
                    str(r["items_succeeded"]),
                    str(r["items_failed"]),
                    f"{r['duration_seconds']:.1f}s",
                )
            console.print(rt)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
