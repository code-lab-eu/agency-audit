"""Country-by-country operational loop orchestrator.

Ties discovery and audit together for continuous processing:

  1. For each country: discover all unprocessed cities
  2. Trigger audits on all newly discovered websites
  3. Run QC checks (suspicious scores, duplicates)
  4. Queue re-audits for websites audited >30 days ago
  5. Track progress via discovery_log and audit_log
  6. Retry failed operations up to 3 times with backoff

CLI entry points:
  - run_country(country_iso): execute full loop for one country
  - run_all_countries(): execute full loop for all active countries
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agency_audit.audit.auditor import audit_website
from agency_audit.db import get_pool
from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient
from agency_audit.loop.qc import run_qc_checks
from agency_audit.loop.reaudit import schedule_reaudits
from agency_audit.loop.retry import retry
from agency_audit.loop.tracking import log_discovery_run, log_full_loop_run

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Per-country full loop
# ──────────────────────────────────────────────────────────────────────


async def run_country(
    country_iso: str,
    max_cities: int | None = None,
    audit_concurrency: int = 3,
    reaudit_interval_days: int = 30,
    reaudit_limit: int = 100,
    skip_discovery: bool = False,
    skip_audit: bool = False,
    skip_qc: bool = False,
    skip_reaudit: bool = False,
) -> dict[str, Any]:
    """Execute the full operational loop for one country:

    discovery → audit → QC → re-audit scheduling → tracking.

    Args:
        country_iso: ISO 3166-1 alpha-2 country code (e.g., 'BG').
        max_cities: Max cities to discover (None = all pending).
        audit_concurrency: Max concurrent audits.
        reaudit_interval_days: Days after which to re-audit.
        reaudit_limit: Max websites to queue for re-audit.
        skip_discovery: Skip the discovery phase.
        skip_audit: Skip the audit phase.
        skip_qc: Skip QC checks.
        skip_reaudit: Skip re-audit scheduling.

    Returns:
        Summary dict with all phase results.
    """
    started_at = time.monotonic()
    country_iso = country_iso.upper()
    errors: list[str] = []

    result: dict[str, Any] = {
        "country": country_iso,
        "phases": {},
        "errors": errors,
    }

    # ── Phase 1: Discovery ──────────────────────────────────────────
    if not skip_discovery:
        logger.info("=== Phase 1: Discovery for %s ===", country_iso)
        phase_start = time.monotonic()

        try:
            pipeline = DiscoveryPipeline(PlacesAPIClient())
            try:
                discovery_result = await pipeline.run_for_countries(
                    country_codes=[country_iso],
                    max_cities_per_country=max_cities or 9999,  # all pending if not capped
                )
            finally:
                await pipeline.close()

            cities_processed = discovery_result.get("cities_processed", 0)
            agencies_found = discovery_result.get("agencies_found", 0)
            result["phases"]["discovery"] = {
                "cities_processed": cities_processed,
                "agencies_found": agencies_found,
                "duration_seconds": round(time.monotonic() - phase_start, 2),
            }

            logger.info(
                "Discovery complete: %d cities, %d agencies",
                cities_processed,
                agencies_found,
            )

            # Log the discovery run
            await log_discovery_run(
                country=country_iso,
                cities_processed=cities_processed,
                agencies_found=agencies_found,
                duration_seconds=time.monotonic() - phase_start,
                errors=errors,
            )
        except Exception as exc:
            logger.error("Discovery phase failed for %s: %s", country_iso, exc)
            errors.append(f"discovery: {exc}")
            result["phases"]["discovery"] = {"error": str(exc)}

    # ── Phase 2: Audit ──────────────────────────────────────────────
    if not skip_audit:
        logger.info("=== Phase 2: Audit for %s ===", country_iso)
        phase_start = time.monotonic()

        try:
            audit_result = await _audit_country_websites(
                country_iso=country_iso,
                concurrency=audit_concurrency,
            )
            result["phases"]["audit"] = {
                "websites_audited": audit_result["audited"],
                "audits_succeeded": audit_result["succeeded"],
                "audits_failed": audit_result["failed"],
                "duration_seconds": round(time.monotonic() - phase_start, 2),
            }

            logger.info(
                "Audit complete: %d succeeded, %d failed",
                audit_result["succeeded"],
                audit_result["failed"],
            )
        except Exception as exc:
            logger.error("Audit phase failed for %s: %s", country_iso, exc)
            errors.append(f"audit: {exc}")
            result["phases"]["audit"] = {"error": str(exc)}
            audit_result = {"audited": 0, "succeeded": 0, "failed": 0}

    # ── Phase 3: QC Checks ──────────────────────────────────────────
    if not skip_qc:
        logger.info("=== Phase 3: QC for %s ===", country_iso)
        phase_start = time.monotonic()

        try:
            qc_result = await run_qc_checks()
            result["phases"]["qc"] = {
                "findings": qc_result["total_findings"],
                "suspicious_scores": qc_result["suspicious_scores"],
                "duplicate_domains": qc_result["duplicate_domains"],
                "duration_seconds": round(time.monotonic() - phase_start, 2),
            }
            logger.info("QC complete: %d findings", qc_result["total_findings"])
        except Exception as exc:
            logger.error("QC phase failed for %s: %s", country_iso, exc)
            errors.append(f"qc: {exc}")
            result["phases"]["qc"] = {"error": str(exc)}
            qc_result = {"total_findings": 0, "suspicious_scores": 0, "duplicate_domains": 0}

    # ── Phase 4: Re-audit Scheduling ────────────────────────────────
    if not skip_reaudit:
        logger.info("=== Phase 4: Re-audit for %s ===", country_iso)
        phase_start = time.monotonic()

        try:
            reaudit_result = await schedule_reaudits(
                interval_days=reaudit_interval_days,
                limit=reaudit_limit,
                country=country_iso,
            )
            result["phases"]["reaudit"] = {
                "queued": reaudit_result["queued"],
                "oldest_age_days": reaudit_result.get("oldest_age_days"),
                "duration_seconds": round(time.monotonic() - phase_start, 2),
            }
            logger.info("Re-audit complete: %d queued", reaudit_result["queued"])
        except Exception as exc:
            logger.error("Re-audit phase failed for %s: %s", country_iso, exc)
            errors.append(f"reaudit: {exc}")
            result["phases"]["reaudit"] = {"error": str(exc)}
            reaudit_result = {"queued": 0, "oldest_age_days": None}

    # ── Log full loop run ───────────────────────────────────────────
    total_duration = time.monotonic() - started_at
    result["duration_seconds"] = round(total_duration, 2)

    try:
        await log_full_loop_run(
            country=country_iso,
            cities_processed=result["phases"].get("discovery", {}).get("cities_processed", 0),
            agencies_discovered=result["phases"].get("discovery", {}).get("agencies_found", 0),
            websites_audited=result["phases"].get("audit", {}).get("websites_audited", 0),
            audits_succeeded=result["phases"].get("audit", {}).get("audits_succeeded", 0),
            audits_failed=result["phases"].get("audit", {}).get("audits_failed", 0),
            qc_findings=result["phases"].get("qc", {}).get("findings", 0),
            reaudit_queued=result["phases"].get("reaudit", {}).get("queued", 0),
            duration_seconds=total_duration,
            errors=errors,
        )
    except Exception as exc:
        logger.warning("Failed to log full loop run: %s", exc)

    logger.info(
        "=== Loop complete for %s: %.1fs | %s ===",
        country_iso,
        total_duration,
        _format_summary(result),
    )
    return result


# ──────────────────────────────────────────────────────────────────────
# All-countries continuous loop
# ──────────────────────────────────────────────────────────────────────


async def run_all_countries(
    max_cities_per_country: int | None = None,
    audit_concurrency: int = 3,
    reaudit_interval_days: int = 30,
    reaudit_limit: int = 100,
    countries: list[str] | None = None,
) -> dict[str, Any]:
    """Execute the full loop for all active countries in sequence.

    Args:
        max_cities_per_country: Max cities per country (None = all pending).
        audit_concurrency: Max concurrent audits per country.
        reaudit_interval_days: Days until re-audit triggers.
        reaudit_limit: Max websites to queue for re-audit per country.
        countries: Specific country codes (None = all active).

    Returns:
        Aggregate summary across all countries.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if countries:
            iso_list = countries
        else:
            rows = await conn.fetch("SELECT iso FROM countries WHERE active = true ORDER BY iso")
            iso_list = [r["iso"] for r in rows]

    logger.info("Running full loop for %d countries: %s", len(iso_list), ", ".join(iso_list))

    all_results: dict[str, Any] = {}
    totals: dict[str, Any] = {
        "countries_processed": 0,
        "cities_processed": 0,
        "agencies_found": 0,
        "websites_audited": 0,
        "audits_succeeded": 0,
        "audits_failed": 0,
        "qc_findings": 0,
        "reaudit_queued": 0,
        "errors": [],
    }

    for i, iso in enumerate(iso_list, 1):
        logger.info("--- Country %d/%d: %s ---", i, len(iso_list), iso)
        try:
            result = await run_country(
                country_iso=iso,
                max_cities=max_cities_per_country,
                audit_concurrency=audit_concurrency,
                reaudit_interval_days=reaudit_interval_days,
                reaudit_limit=reaudit_limit,
            )
            all_results[iso] = result

            # Aggregate totals
            totals["countries_processed"] += 1
            disc = result["phases"].get("discovery", {})
            audit = result["phases"].get("audit", {})
            qc = result["phases"].get("qc", {})
            reaudit = result["phases"].get("reaudit", {})

            totals["cities_processed"] += disc.get("cities_processed", 0)
            totals["agencies_found"] += disc.get("agencies_found", 0)
            totals["websites_audited"] += audit.get("websites_audited", 0)
            totals["audits_succeeded"] += audit.get("audits_succeeded", 0)
            totals["audits_failed"] += audit.get("audits_failed", 0)
            totals["qc_findings"] += qc.get("findings", 0)
            totals["reaudit_queued"] += reaudit.get("queued", 0)
            totals["errors"].extend(result.get("errors", []))

        except Exception as exc:
            logger.error("Fatal error processing %s: %s", iso, exc)
            all_results[iso] = {"error": str(exc)}
            totals["errors"].append(f"{iso}: {exc}")

    logger.info("=== All-countries complete ===")
    logger.info("Totals: %s", _format_totals(totals))

    return {"results": all_results, "totals": totals}


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────


async def _audit_country_websites(
    country_iso: str,
    concurrency: int = 3,
) -> dict[str, int]:
    """Audit all pending websites for a country.

    Uses semaphore-limited concurrency and retries failed audits 3 times.

    Returns:
        Dict with 'audited', 'succeeded', 'failed' counts.
    """
    pool = await get_pool()

    # Fetch all pending websites for this country
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT w.id, w.url
               FROM websites w
               JOIN website_cities wc ON wc.website_id = w.id
               JOIN cities c ON c.id = wc.city_id
               WHERE c.country = $1 AND w.audit_status = 'pending'
                 AND w.audit_attempts < 3
               ORDER BY w.id""",
            country_iso,
        )

    if not rows:
        logger.info("No pending websites to audit for %s", country_iso)
        return {"audited": 0, "succeeded": 0, "failed": 0}

    logger.info("Auditing %d websites for %s (concurrency=%d)", len(rows), country_iso, concurrency)

    semaphore = asyncio.Semaphore(concurrency)
    succeeded = 0
    failed = 0
    errors: list[str] = []

    async def _audit_one(wid: int, url: str) -> None:
        nonlocal succeeded, failed
        async with semaphore:
            try:
                # Retry up to 3 times
                audit_result = await retry(
                    audit_website,
                    url,
                    max_attempts=3,
                    base_delay=2.0,
                )

                # Store result in DB
                import json

                async with pool.acquire() as c:
                    await c.execute(
                        """UPDATE websites
                           SET audit_data = $1::jsonb,
                               score = $2,
                               audit_status = 'audited',
                               last_audited_at = now(),
                               audit_attempts = audit_attempts + 1,
                               audit_last_error = NULL
                           WHERE id = $3""",
                        json.dumps(audit_result.to_dict()),
                        audit_result.score,
                        wid,
                    )
                succeeded += 1
                logger.info("Audit succeeded: %s (score=%d)", url, audit_result.score)

            except Exception as exc:
                failed += 1
                error_msg = f"{url}: {exc}"
                errors.append(error_msg)
                logger.error("Audit failed for %s: %s", url, exc)

                # Mark as failed after retries exhausted
                async with pool.acquire() as c:
                    await c.execute(
                        """UPDATE websites
                           SET audit_status = 'failed',
                               audit_last_error = $1,
                               audit_attempts = audit_attempts + 1
                           WHERE id = $2""",
                        str(exc)[:500],
                        wid,
                    )

    tasks = [_audit_one(row["id"], row["url"]) for row in rows]
    await asyncio.gather(*tasks)

    return {
        "audited": len(rows),
        "succeeded": succeeded,
        "failed": failed,
    }


def _format_summary(result: dict[str, Any]) -> str:
    """Format a run_country result as a compact string."""
    parts = []
    disc = result["phases"].get("discovery", {})
    if disc:
        parts.append(
            f"discovery:{disc.get('cities_processed', 0)}c/{disc.get('agencies_found', 0)}a"
        )

    audit = result["phases"].get("audit", {})
    if audit:
        parts.append(f"audit:{audit.get('succeeded', 0)}✓/{audit.get('failed', 0)}✗")

    qc = result["phases"].get("qc", {})
    if qc:
        parts.append(f"qc:{qc.get('findings', 0)}")

    reaudit = result["phases"].get("reaudit", {})
    if reaudit:
        parts.append(f"reaudit:{reaudit.get('queued', 0)}q")

    errors = result.get("errors", [])
    if errors:
        parts.append(f"errors:{len(errors)}")

    return " | ".join(parts)


def _format_totals(totals: dict[str, Any]) -> str:
    """Format totals as a compact string."""
    return (
        f"{totals['countries_processed']} countries | "
        f"{totals['cities_processed']} cities | "
        f"{totals['agencies_found']} agencies | "
        f"{totals['audits_succeeded']}✓/{totals['audits_failed']}✗ audits | "
        f"{totals['qc_findings']} qc | "
        f"{totals['reaudit_queued']} reaudits"
    )
