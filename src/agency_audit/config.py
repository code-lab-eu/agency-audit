"""Application configuration via pydantic-settings."""

from urllib.parse import quote

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

    @property
    def dsn(self) -> str:
        """Asyncpg connection DSN with URL-encoded credentials."""
        user = quote(self.pg_user, safe="")
        auth = user
        if self.pg_password:
            password = quote(self.pg_password, safe="")
            auth = f"{user}:{password}"
        return f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"


settings = Settings()
