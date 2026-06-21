"""Website audit pipeline — evaluates discovered real estate agency websites.

Modules:
  models           — data classes for audit results
  robots           — robots.txt fetch & parse
  anti_scraping    — Cloudflare/reCAPTCHA/JS-only detection
  api_detection    — GraphQL/REST/JSON-LD detection
  property_count   — listing count estimation
  listing_quality  — structured data, images, prices, etc.
  tech_stack       — framework/hosting/CDN identification
  scoring          — configurable scoring formula (0-100)
  auditor          — main orchestrator combining all checks
  playwright_fetch — on-demand JS rendering for JS-heavy sites
"""

from agency_audit.audit.auditor import audit_website, audit_websites
from agency_audit.audit.models import AuditData
from agency_audit.audit.scoring import compute_score, load_scoring_config

__all__ = [
    "audit_website",
    "audit_websites",
    "AuditData",
    "compute_score",
    "load_scoring_config",
]
