"""Quality control checks for discovered and audited websites.

Detects:
  - Suspicious scores (0 or 100 — likely errors or insufficient data)
  - Duplicate domains across different cities
  - Websites needing manual review

Flags issues on the websites table via needs_review / review_reason / qc_checks columns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from agency_audit.db import get_pool

logger = logging.getLogger(__name__)


@dataclass
class QCFinding:
    """A quality control finding for a website."""

    website_id: int
    url: str
    reason: str
    severity: str = "warning"  # "warning" or "error"


# ──────────────────────────────────────────────────────────────────────
# Suspicious score detection
# ──────────────────────────────────────────────────────────────────────


async def flag_suspicious_scores() -> list[QCFinding]:
    """Flag websites with suspicious scores (0 or 100).

    Score 0: Usually means the audit didn't gather enough data (no robots.txt,
    no homepage content, etc.) or the site is simply inaccessible.
    Score 100: Could be a legitimate top-tier site, but should be verified
    (potential false positive from default values).

    Returns:
        List of QC findings generated.
    """
    pool = await get_pool()
    findings: list[QCFinding] = []

    async with pool.acquire() as conn:
        # Find websites with score 0 or 100 that haven't been QC-checked yet
        rows = await conn.fetch(
            """SELECT id, url, score, audit_status
               FROM websites
               WHERE (score = 0 OR score = 100)
                 AND audit_status = 'audited'
                 AND (needs_review = false OR qc_checks = '[]'::jsonb)"""
        )

        for row in rows:
            wid = row["id"]
            score = row["score"]
            url = row["url"]

            if score == 0:
                reason = "Suspicious score 0 — possible audit failure or no usable data"
            else:
                reason = "Suspicious score 100 — verify audit is genuine (not default values)"

            findings.append(QCFinding(website_id=wid, url=url, reason=reason, severity="warning"))

            # Mark in database
            await conn.execute(
                """UPDATE websites
                   SET needs_review = true,
                       review_reason = COALESCE(review_reason, '') || $1 || '; ',
                       qc_checks = qc_checks || $2::jsonb
                   WHERE id = $3""",
                reason,
                json.dumps([{"check": "suspicious_score", "finding": reason}]),
                wid,
            )

    logger.info("QC: flagged %d websites with suspicious scores", len(findings))
    return findings


# ──────────────────────────────────────────────────────────────────────
# Duplicate detection
# ──────────────────────────────────────────────────────────────────────


def _extract_domain(url: str) -> str:
    """Extract the normalized domain from a URL."""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path.split("/")[0]
    domain = domain.lower()
    # Remove www. prefix for comparison
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


async def detect_duplicates() -> list[QCFinding]:
    """Detect websites with the same domain appearing in multiple cities.

    A real estate agency might have branches in many cities with the same
    corporate website. This is normal but should be flagged so we don't
    count it as a new discovery each time.

    Returns:
        List of QC findings (one per duplicate group).
    """
    pool = await get_pool()
    findings: list[QCFinding] = []

    async with pool.acquire() as conn:
        # Find domains that appear for multiple cities
        rows = await conn.fetch(
            """SELECT w.url,
                      COUNT(DISTINCT wc.city_id) AS city_count,
                      ARRAY_AGG(DISTINCT c.label ORDER BY c.label) AS cities,
                      ARRAY_AGG(DISTINCT w.id ORDER BY w.id) AS website_ids
               FROM websites w
               JOIN website_cities wc ON wc.website_id = w.id
               JOIN cities c ON c.id = wc.city_id
               WHERE w.audit_status = 'audited'
               GROUP BY w.url
               HAVING COUNT(DISTINCT wc.city_id) > 1
               ORDER BY city_count DESC"""
        )

        for row in rows:
            url = row["url"]
            cities = row["cities"]
            website_ids = row["website_ids"]
            city_count = row["city_count"]

            reason = f"Same domain in {city_count} cities: {', '.join(cities)}"
            findings.append(
                QCFinding(
                    website_id=website_ids[0],
                    url=url,
                    reason=reason,
                    severity="info",
                )
            )

            # Mark all website entries for review
            for wid in website_ids:
                await conn.execute(
                    """UPDATE websites
                       SET needs_review = true,
                           review_reason = COALESCE(review_reason, '') || $1 || '; ',
                           qc_checks = qc_checks || $2::jsonb
                       WHERE id = $3""",
                    reason,
                    json.dumps([{"check": "duplicate_domain", "finding": reason}]),
                    wid,
                )

    logger.info("QC: detected %d duplicate domain groups", len(findings))
    return findings


# ──────────────────────────────────────────────────────────────────────
# Manual review flagging
# ──────────────────────────────────────────────────────────────────────


async def mark_for_manual_review(
    website_id: int, reason: str, severity: str = "error"
) -> None:
    """Flag a website for manual review.

    Args:
        website_id: The website ID.
        reason: Why manual review is needed.
        severity: 'warning' or 'error'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE websites
               SET needs_review = true,
                   review_reason = COALESCE(review_reason, '') || $1 || '; ',
                   qc_checks = qc_checks || $2::jsonb
               WHERE id = $3""",
            reason,
            json.dumps([{"check": "manual_review", "severity": severity, "finding": reason}]),
            website_id,
        )
    logger.info("QC: flagged website %d for manual review: %s", website_id, reason)


# ──────────────────────────────────────────────────────────────────────
# Full QC run
# ──────────────────────────────────────────────────────────────────────


async def run_qc_checks() -> dict[str, Any]:
    """Run all quality control checks and return a summary.

    Returns:
        Summary dict with counts of each finding type.
    """
    logger.info("Running all QC checks...")

    suspicious = await flag_suspicious_scores()
    duplicates = await detect_duplicates()

    summary = {
        "suspicious_scores": len(suspicious),
        "duplicate_domains": len(duplicates),
        "total_findings": len(suspicious) + len(duplicates),
    }

    logger.info("QC complete: %s", summary)
    return summary


async def get_websites_needing_review() -> list[dict[str, Any]]:
    """Get all websites flagged for manual review.

    Returns:
        List of dicts with website info and review reason.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, url, label, score, review_reason, qc_checks
               FROM websites
               WHERE needs_review = true
               ORDER BY score DESC"""
        )

    return [
        {
            "id": row["id"],
            "url": row["url"],
            "label": row["label"],
            "score": row["score"],
            "review_reason": row["review_reason"],
            "qc_checks": row["qc_checks"],
        }
        for row in rows
    ]
