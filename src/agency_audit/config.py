"""Application configuration via pydantic-settings.

All configurable values live here with sensible defaults.
Secrets (API keys, passwords) default to empty strings -- production
deployments MUST set them via environment variables or .env file.

A .env file in the working directory is loaded automatically by pydantic-settings.
Copy .env.example to .env and fill in the required values.

Environment variables are prefixed with AGENCY_AUDIT_ (e.g. AGENCY_AUDIT_PG_HOST).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENCY_AUDIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- PostgreSQL connection ------------------------------------------------
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "agency_audit"
    pg_password: str = ""
    pg_database: str = "agency_audit"

    # -- PostgreSQL pool tuning -----------------------------------------------
    pg_pool_min_size: int = 2
    pg_pool_max_size: int = 10
    pg_pool_command_timeout: int = 30

    # -- Geonames import ------------------------------------------------------
    geonames_min_population: int = 50000
    geonames_url: str = "https://download.geonames.org/export/dump/cities15000.zip"
    geonames_download_timeout: int = 120

    # -- Google Maps Places API -----------------------------------------------
    google_maps_api_key: str = ""
    places_api_timeout: int = 30
    places_radius_meters: int = 10000
    places_max_results: int = 60
    places_rate_limit_qps: float = 5.0

    # -- Audit HTTP defaults --------------------------------------------------
    user_agent: str = "AgencyAuditBot/1.0"
    audit_timeout: int = 30
    robots_timeout: int = 10
    audit_http_timeout: int = 15
    sitemap_timeout: int = 20
    socket_connect_timeout: int = 10
    audit_concurrency: int = 5

    # -- Playwright browser fetcher -------------------------------------------
    playwright_timeout_ms: int = 30000
    playwright_wait_seconds: float = 3.0

    # -- Web dashboard server -------------------------------------------------
    serve_host: str = "127.0.0.1"
    serve_port: int = 8000

    # -- Scoring config -------------------------------------------------------
    scoring_config_path: str = ""

    # Structured JSON logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    # ── Places tiling / discovery knobs ──────────────────────
    # How many layers of recursive quadtree subdivision before giving up
    places_tile_max_depth: int = 3
    # Minimum result count in a tile to consider it "saturated" (stop subdividing)
    places_tile_saturation_threshold: int = 60
    # Hard cap on Places API calls per city during tiled discovery
    places_max_calls_per_city: int = 200
    # Fallback bounding-box half-extent in metres (used when city boundaries unknown)
    places_city_half_extent_meters: int = 15000

    # ── Places tiling / discovery knobs ──────────────────────
    # How many layers of recursive quadtree subdivision before giving up
    places_tile_max_depth: int = 3
    # Minimum result count in a tile to consider it "saturated" (stop subdividing)
    places_tile_saturation_threshold: int = 60
    # Hard cap on Places API calls per city during tiled discovery
    places_max_calls_per_city: int = 200
    # Fallback bounding-box half-extent in metres (used when city boundaries unknown)
    places_city_half_extent_meters: int = 15000

    @property
    def dsn(self) -> str:
        """Asyncpg connection DSN with URL-encoded credentials."""
        user = quote(self.pg_user, safe="")
        auth = user
        if self.pg_password:
            auth = f"{user}:{quote(self.pg_password, safe='')}"
        return f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"

    # -- Validation -----------------------------------------------------------

    @model_validator(mode="after")
    def _validate_critical_settings(self) -> Settings:
        """Log warnings for settings that are empty/default in production.

        This does NOT hard-fail at import time because config is loaded
        by every module -- even during testing where DB/API aren't needed.
        Call ``settings.ensure_ready_for(...)`` at the appropriate entry
        point to fail fast when a required capability is missing.
        """
        if not self.google_maps_api_key:
            logger.info(
                "AGENCY_AUDIT_GOOGLE_MAPS_API_KEY is not set -- "
                "discovery via Places API will not work"
            )
        if not self.pg_password:
            logger.info(
                "AGENCY_AUDIT_PG_PASSWORD is not set -- "
                "using password-less authentication (fine for local dev, "
                "not recommended for production)"
            )
        if self.audit_timeout <= 0:
            logger.warning(
                "AGENCY_AUDIT_AUDIT_TIMEOUT is %d -- must be positive; "
                "audits may fail with timeout errors",
                self.audit_timeout,
            )
        if self.pg_pool_min_size < 1:
            logger.warning(
                "AGENCY_AUDIT_PG_POOL_MIN_SIZE is %d -- must be >= 1; "
                "using default min pool size of 2",
                self.pg_pool_min_size,
            )
        return self

    def ensure_ready_for(self, capability: str) -> None:
        """Fail fast if required configuration for *capability* is missing.

        Capabilities:
            ``"db"``       -- PostgreSQL connectivity
            ``"discovery"`` -- Google Maps Places API (raises if API key absent)
            ``"audit"``    -- Audit pipeline (checks timeouts are reasonable)
            ``"all"``      -- Everything above
        """
        checks: list[tuple[Any, str, str]] = []

        if capability in ("db", "all"):
            try:
                from urllib.parse import urlparse

                parsed = urlparse(self.dsn)
                if not parsed.hostname:
                    checks.append((self.pg_host, "pg_host is empty or invalid", "pg_host"))
            except Exception:
                checks.append((self.dsn, f"DSN '{self.dsn}' is not parseable", "DSN"))

        if capability in ("discovery", "all") and not self.google_maps_api_key:
            checks.append(
                (
                    self.google_maps_api_key,
                    "Google Maps API key is not set",
                    "google_maps_api_key",
                )
            )

        if capability in ("audit", "all"):
            if self.audit_timeout < 1:
                checks.append(
                    (
                        self.audit_timeout,
                        "audit_timeout must be >= 1",
                        "audit_timeout",
                    )
                )
            if self.audit_concurrency < 1:
                checks.append(
                    (
                        self.audit_concurrency,
                        "audit_concurrency must be >= 1",
                        "audit_concurrency",
                    )
                )

        if checks:
            missing = "; ".join(f"{name}: {reason}" for (_, reason, name) in checks)
            raise RuntimeError(f"Configuration validation failed: {missing}")


settings = Settings()
