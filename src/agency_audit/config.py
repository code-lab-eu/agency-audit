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

    # Structured JSON logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"

    @property
    def dsn(self) -> str:
        """Asyncpg connection DSN."""
        auth = self.pg_user
        if self.pg_password:
            auth = f"{self.pg_user}:{self.pg_password}"
        return f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"


settings = Settings()
