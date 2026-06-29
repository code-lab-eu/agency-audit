"""Configurable scoring formula for website audits.

The scoring config is externalized in a YAML file (scoring_config.yaml)
so weights can be adjusted without code changes.

At runtime the loader checks paths in this order:

1. ``AGENCY_AUDIT_SCORING_CONFIG_PATH`` env var (explicit override)
2. ``scoring_config.yaml`` in the current working directory (dev convenience)
3. ``config/scoring_config.yaml``
4. Repo root (for development checkouts)
5. Packaged ``scoring_config.yaml`` inside the package (final fallback)

The first valid dict wins; missing / unparseable files are skipped with a warning.

Score is a signed integer (0-100 typical, negative possible for unsuitable sites).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from agency_audit.audit.models import AuditData
from agency_audit.config import settings

logger = logging.getLogger(__name__)

# Default scoring config — used if no YAML config file is found
DEFAULT_CONFIG: dict = {
    # robots.txt
    "robots_allows": 20,
    "robots_disallows": -50,
    # anti-scraping
    "has_anti_scraping": -20,
    # API detection
    "has_api": 20,
    "has_graphql_api": 25,  # bonus for GraphQL
    # property count (first matching tier is applied)
    "property_count_tiers": [
        {"min": 1000, "points": 30},
        {"min": 500, "points": 20},
        {"min": 100, "points": 10},
        {"min": 10, "points": 5},
    ],
    # listing quality (each check)
    "has_structured_data": 10,
    "listings_have_prices": 10,
    "listings_have_locations": 10,
    "listings_have_images": 5,
    "listings_have_descriptions": 5,
    "has_property_map": 5,
    # performance
    "response_time_fast": 5,  # < 500ms
    "response_time_slow": -5,  # > 3000ms
    # SSL
    "ssl_valid": 5,
    "ssl_invalid": -20,
    # clamp
    "min_score": -100,
    "max_score": 100,
}

# Paths tried in order — first match wins.
# 1. CWD (convenience override during dev)
# 2. CWD/config/
# 3. Repo root  (four levels up from src/agency_audit/audit/scoring.py)
# 4. Package dir (ships with the wheel — final fallback)
CONFIG_FILE_PATHS = [
    Path("scoring_config.yaml"),
    Path("config/scoring_config.yaml"),
    Path(__file__).parent.parent.parent.parent / "scoring_config.yaml",
    Path(__file__).parent / "scoring_config.yaml",
]


def _try_load_config(path: Path) -> dict | None:
    """Try to load a scoring config dict from a YAML file at *path*.

    Returns the merged config on success, ``None`` if the file cannot
    be parsed as a dict.  Malformed YAML and non-dict content both
    produce a warning and return ``None``.
    """
    try:
        with open(path) as f:
            user_config = yaml.safe_load(f)
    except yaml.YAMLError, OSError:
        logger.warning(
            "Failed to parse %s — skipping",
            path,
            exc_info=True,
        )
        return None

    if not isinstance(user_config, dict):
        logger.warning(
            "Scoring config at %s is not a dict (got %s) — skipping",
            path,
            type(user_config).__name__,
        )
        return None

    merged = DEFAULT_CONFIG.copy()
    merged.update(user_config)
    return merged


def load_scoring_config() -> dict:
    """Load scoring config from YAML file, or fall back to defaults.

    Checks ``AGENCY_AUDIT_SCORING_CONFIG_PATH`` first (if configured),
    then searches standard paths for ``scoring_config.yaml``.  If no
    usable file is found, returns the hard-coded defaults without an
    error.  Unparseable YAML or non-dict content in any candidate file
    produces a warning but does not stop the search — the next path is
    tried.
    """
    # 1. Explicit path via env var
    if settings.scoring_config_path:
        explicit = Path(settings.scoring_config_path)
        if explicit.exists():
            config = _try_load_config(explicit)
            if config is not None:
                return config
        else:
            logger.warning(
                "AGENCY_AUDIT_SCORING_CONFIG_PATH is set to %s but the file does not exist",
                settings.scoring_config_path,
            )

    # 2. Standard search paths
    for path in CONFIG_FILE_PATHS:
        if path.exists():
            config = _try_load_config(path)
            if config is not None:
                return config

    return DEFAULT_CONFIG.copy()


def compute_score(audit: AuditData, config: dict | None = None) -> tuple[int, dict[str, int]]:
    """Compute the overall score from audit data using the scoring config.

    Args:
        audit: Complete AuditData with all check results.
        config: Optional scoring config dict. If not provided, loads from file.

    Returns:
        (total_score, breakdown) where breakdown maps check name to points.
    """
    if config is None:
        config = load_scoring_config()

    breakdown: dict[str, int] = {}
    score = 0

    # robots.txt
    if audit.robots.allows_scraping:
        breakdown["robots_allows"] = config["robots_allows"]
        score += config["robots_allows"]
    else:
        breakdown["robots_disallows"] = config["robots_disallows"]
        score += config["robots_disallows"]

    # anti-scraping
    if audit.anti_scraping.detected:
        breakdown["has_anti_scraping"] = config["has_anti_scraping"]
        score += config["has_anti_scraping"]

    # API detection
    if audit.api_detection.detected:
        if audit.api_detection.api_type == "graphql":
            breakdown["has_graphql_api"] = config["has_graphql_api"]
            score += config["has_graphql_api"]
        else:
            breakdown["has_api"] = config["has_api"]
            score += config["has_api"]

    # property count (tiered — first match wins)
    count = audit.property_count.count
    for tier in config["property_count_tiers"]:
        if count >= tier["min"]:
            breakdown[f"property_count_{tier['min']}+"] = tier["points"]
            score += tier["points"]
            break

    # listing quality
    if audit.listing_quality.has_structured_data:
        breakdown["has_structured_data"] = config["has_structured_data"]
        score += config["has_structured_data"]

    if audit.listing_quality.has_prices:
        breakdown["listings_have_prices"] = config["listings_have_prices"]
        score += config["listings_have_prices"]

    if audit.listing_quality.has_locations:
        breakdown["listings_have_locations"] = config["listings_have_locations"]
        score += config["listings_have_locations"]

    if audit.listing_quality.has_images:
        breakdown["listings_have_images"] = config["listings_have_images"]
        score += config["listings_have_images"]

    if audit.listing_quality.has_descriptions:
        breakdown["listings_have_descriptions"] = config["listings_have_descriptions"]
        score += config["listings_have_descriptions"]

    if audit.listing_quality.has_property_map:
        breakdown["has_property_map"] = config["has_property_map"]
        score += config["has_property_map"]

    # performance
    if audit.response_time_ms is not None:
        if audit.response_time_ms < 500:
            breakdown["response_time_fast"] = config["response_time_fast"]
            score += config["response_time_fast"]
        elif audit.response_time_ms > 3000:
            breakdown["response_time_slow"] = config["response_time_slow"]
            score += config["response_time_slow"]

    # SSL
    if audit.ssl_valid:
        breakdown["ssl_valid"] = config["ssl_valid"]
        score += config["ssl_valid"]
    else:
        breakdown["ssl_invalid"] = config["ssl_invalid"]
        score += config["ssl_invalid"]

    # Clamp to range
    score = max(config["min_score"], min(config["max_score"], score))

    return score, breakdown
