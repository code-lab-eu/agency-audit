"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENCY_AUDIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL connection
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "agency_audit"
    pg_password: str = ""
    pg_database: str = "agency_audit"

    # Geonames import
    geonames_min_population: int = 50000
    geonames_url: str = "https://download.geonames.org/export/dump/cities15000.zip"

    # Google Maps Places API key, used by the discovery pipeline
    google_maps_api_key: str = ""

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
        """Asyncpg connection DSN."""
        auth = self.pg_user
        if self.pg_password:
            auth = f"{self.pg_user}:{self.pg_password}"
        return f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"


settings = Settings()
