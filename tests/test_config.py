"""Tests for agency_audit.config module."""

import pytest

from agency_audit.config import Settings


def _dsn_parts(dsn):
    """Parse a postgresql DSN into components for safe comparison."""
    # postgresql://user:password@host:port/db or postgresql://user@host:port/db
    rest = dsn.replace("postgresql://", "", 1)
    user_part, rest = rest.split("@", 1)
    if ":" in user_part:
        user, password = user_part.split(":", 1)
    else:
        user = user_part
        password = ""
    host_part, db = rest.rsplit("/", 1)
    host, port_str = host_part.rsplit(":", 1)
    return {
        "user": user,
        "password": password,
        "host": host,
        "port": int(port_str),
        "db": db,
    }


class TestSettingsDSN:
    """Tests for the Settings.dsn property."""

    def test_dsn_no_password(self):
        s = Settings(
            pg_host="testhost",
            pg_port=5432,
            pg_user="testuser",
            pg_password="",
            pg_database="testdb",
        )
        parts = _dsn_parts(s.dsn)
        assert parts["user"] == "testuser"
        assert parts["password"] == ""
        assert parts["host"] == "testhost"
        assert parts["port"] == 5432
        assert parts["db"] == "testdb"

    def test_dsn_with_password(self):
        s = Settings(
            pg_host="testhost",
            pg_port=5432,
            pg_user="testuser",
            pg_password="testpass",
            pg_database="testdb",
        )
        parts = _dsn_parts(s.dsn)
        assert parts["user"] == "testuser"
        assert parts["password"] == "testpass"
        assert parts["host"] == "testhost"
        assert parts["port"] == 5432
        assert parts["db"] == "testdb"

    def test_dsn_default_values(self):
        s = Settings(
            pg_host="localhost",
            pg_port=5432,
            pg_user="agency_audit",
            pg_password="",
            pg_database="agency_audit",
        )
        parts = _dsn_parts(s.dsn)
        assert parts["user"] == "agency_audit"
        assert parts["password"] == ""
        assert parts["host"] == "localhost"
        assert parts["port"] == 5432
        assert parts["db"] == "agency_audit"

    def test_dsn_starts_with_postgresql(self):
        s = Settings()
        assert s.dsn.startswith("postgresql://")


class TestSettingsValidation:
    """Tests for ensure_ready_for."""

    def test_ensure_ready_db_passes(self):
        s = Settings()
        s.ensure_ready_for("db")

    def test_ensure_ready_discovery_fails_without_key(self):
        s = Settings(google_maps_api_key="")
        with pytest.raises(RuntimeError, match="Google Maps API key"):
            s.ensure_ready_for("discovery")

    def test_ensure_ready_discovery_passes_with_key(self):
        s = Settings(google_maps_api_key="some-key")
        s.ensure_ready_for("discovery")

    def test_ensure_ready_audit_fails_bad_timeout(self):
        s = Settings(audit_timeout=0)
        with pytest.raises(RuntimeError, match="audit_timeout"):
            s.ensure_ready_for("audit")

    def test_ensure_ready_audit_fails_bad_concurrency(self):
        s = Settings(audit_concurrency=0)
        with pytest.raises(RuntimeError, match="audit_concurrency"):
            s.ensure_ready_for("audit")

    def test_ensure_ready_audit_passes(self):
        s = Settings(audit_timeout=30, audit_concurrency=5)
        s.ensure_ready_for("audit")

    def test_ensure_ready_all_passes(self):
        s = Settings(google_maps_api_key="key", audit_timeout=30, audit_concurrency=5)
        s.ensure_ready_for("all")


class TestSettingsDefaults:
    """Tests for default configuration values."""

    def test_pg_defaults(self):
        s = Settings(
            pg_host="localhost",
            pg_port=5432,
            pg_user="agency_audit",
            pg_password="",
            pg_database="agency_audit",
        )
        assert s.pg_host == "localhost"
        assert s.pg_port == 5432
        assert s.pg_user == "agency_audit"
        assert s.pg_password == ""
        assert s.pg_database == "agency_audit"

    def test_pool_tuning(self):
        s = Settings()
        assert s.pg_pool_min_size == 2
        assert s.pg_pool_max_size == 10
        assert s.pg_pool_command_timeout == 30

    def test_geonames_defaults(self):
        s = Settings()
        assert s.geonames_min_population == 50000
        assert "geonames" in s.geonames_url

    def test_places_api_defaults(self):
        s = Settings()
        assert s.places_api_timeout == 30
        assert s.places_radius_meters == 10000
        assert s.places_max_results == 60
        assert s.places_rate_limit_qps == 5.0

    def test_audit_timeout_defaults(self):
        s = Settings()
        assert s.audit_timeout == 30
        assert s.robots_timeout == 10
        assert s.audit_http_timeout == 15
        assert s.sitemap_timeout == 20
        assert s.socket_connect_timeout == 10
        assert s.audit_concurrency == 5

    def test_serve_defaults(self):
        s = Settings()
        assert s.serve_host == "127.0.0.1"
        assert s.serve_port == 8000

    def test_playwright_defaults(self):
        s = Settings()
        assert s.playwright_timeout_ms == 30000
        assert s.playwright_wait_seconds == 3.0

    def test_scoring_config_path(self):
        assert Settings().scoring_config_path == ""
        assert Settings(scoring_config_path="/x.yaml").scoring_config_path == "/x.yaml"

    def test_module_level_settings(self):
        from agency_audit.config import settings

        assert isinstance(settings, Settings)

    def test_user_agent_default(self):
        assert Settings().user_agent == "AgencyAuditBot/1.0"

    def test_google_maps_api_key_default(self):
        assert Settings().google_maps_api_key == ""

    def test_pg_pool_min_size_warns_on_invalid(self, caplog):
        """Settings with pg_pool_min_size < 1 should log a warning."""
        import logging

        caplog.set_level(logging.WARNING)
        Settings(pg_pool_min_size=0)
        assert "PG_POOL_MIN_SIZE" in caplog.text

    def test_ensure_ready_db_fails_with_empty_host(self):
        """ensure_ready_for('db') should fail when pg_host is empty."""
        s = Settings(pg_host="")
        with pytest.raises(RuntimeError, match="pg_host"):
            s.ensure_ready_for("db")


class TestSettingsEnvFile:
    """Tests for loading settings from a .env file."""

    def test_env_file_loading(self, tmp_path, monkeypatch):
        """Settings loaded from a .env file should reflect file values."""
        # Clear env vars that would take precedence over .env file values
        for var in (
            "AGENCY_AUDIT_PG_HOST",
            "AGENCY_AUDIT_PG_PORT",
            "AGENCY_AUDIT_LOG_LEVEL",
            "AGENCY_AUDIT_PLACES_MAX_RESULTS",
        ):
            monkeypatch.delenv(var, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "AGENCY_AUDIT_PG_HOST=envfilehost\n"
            "AGENCY_AUDIT_PG_PORT=9999\n"
            "AGENCY_AUDIT_LOG_LEVEL=DEBUG\n"
            "AGENCY_AUDIT_PLACES_MAX_RESULTS=42\n"
        )
        monkeypatch.chdir(tmp_path)
        s = Settings(_env_file=env_file)
        assert s.pg_host == "envfilehost"
        assert s.pg_port == 9999
        assert s.log_level == "DEBUG"
        assert s.places_max_results == 42

    def test_env_file_partial_overrides(self, tmp_path, monkeypatch):
        """A .env file with partial settings keeps defaults for unset fields."""
        monkeypatch.delenv("AGENCY_AUDIT_LOG_LEVEL", raising=False)
        monkeypatch.delenv("AGENCY_AUDIT_PG_HOST", raising=False)
        monkeypatch.delenv("AGENCY_AUDIT_PG_PORT", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("AGENCY_AUDIT_LOG_LEVEL=WARNING\n")
        monkeypatch.chdir(tmp_path)
        s = Settings(_env_file=env_file)
        assert s.log_level == "WARNING"
        # Defaults remain for unset fields
        assert s.pg_host == "localhost"
        assert s.pg_port == 5432

    def test_env_file_with_empty_values(self, tmp_path, monkeypatch):
        """Empty values in .env file should be treated as empty strings."""
        monkeypatch.delenv("AGENCY_AUDIT_GOOGLE_MAPS_API_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("AGENCY_AUDIT_GOOGLE_MAPS_API_KEY=\n")
        monkeypatch.chdir(tmp_path)
        s = Settings(_env_file=env_file)
        assert s.google_maps_api_key == ""


class TestSettingsEnvOverride:
    """Tests for environment variable overrides taking precedence."""

    def test_env_var_overrides_default(self, monkeypatch):
        """Environment variables should override the default values."""
        monkeypatch.setenv("AGENCY_AUDIT_PG_HOST", "envhost")
        monkeypatch.setenv("AGENCY_AUDIT_LOG_LEVEL", "ERROR")
        s = Settings()
        assert s.pg_host == "envhost"
        assert s.log_level == "ERROR"

    def test_env_var_overrides_env_file(self, tmp_path, monkeypatch):
        """Environment variables should override .env file values."""
        env_file = tmp_path / ".env"
        env_file.write_text("AGENCY_AUDIT_PG_HOST=filehost\nAGENCY_AUDIT_LOG_LEVEL=WARNING\n")
        monkeypatch.setenv("AGENCY_AUDIT_PG_HOST", "envhost")
        monkeypatch.setenv("AGENCY_AUDIT_LOG_LEVEL", "ERROR")
        monkeypatch.chdir(tmp_path)
        s = Settings(_env_file=env_file)
        assert s.pg_host == "envhost"
        assert s.log_level == "ERROR"

    def test_mixed_env_and_defaults(self, monkeypatch):
        """Only overridden env vars change; everything else stays default."""
        # Clear env vars that may be set in Docker/CI environments
        monkeypatch.delenv("AGENCY_AUDIT_PG_HOST", raising=False)
        monkeypatch.delenv("AGENCY_AUDIT_LOG_LEVEL", raising=False)
        monkeypatch.setenv("AGENCY_AUDIT_AUDIT_CONCURRENCY", "25")
        s = Settings()
        assert s.audit_concurrency == 25
        assert s.pg_host == "localhost"
        assert s.log_level == "INFO"

    def test_int_env_var_parsing(self, monkeypatch):
        """Integer fields should be parsed from string env var values."""
        monkeypatch.setenv("AGENCY_AUDIT_PG_PORT", "7777")
        monkeypatch.setenv("AGENCY_AUDIT_AUDIT_CONCURRENCY", "12")
        s = Settings()
        assert s.pg_port == 7777
        assert isinstance(s.pg_port, int)
        assert s.audit_concurrency == 12
        assert isinstance(s.audit_concurrency, int)

    def test_float_env_var_parsing(self, monkeypatch):
        """Float fields should be parsed from string env var values."""
        monkeypatch.setenv("AGENCY_AUDIT_PLACES_RATE_LIMIT_QPS", "3.5")
        s = Settings()
        assert s.places_rate_limit_qps == 3.5
        assert isinstance(s.places_rate_limit_qps, float)


class TestGetSettings:
    """Tests for the get_settings() dependency injection function."""

    def test_returns_settings_instance(self):
        from agency_audit.config import get_settings

        s = get_settings()
        assert isinstance(s, Settings)

    def test_returns_singleton(self):
        from agency_audit.config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_can_be_patched_for_testing(self, monkeypatch):
        """Tests can inject custom Settings by patching the module-level singleton."""
        from agency_audit import config

        custom = Settings(pg_host="customhost", pg_port=9999)
        monkeypatch.setattr(config, "settings", custom)
        s = config.get_settings()
        assert s.pg_host == "customhost"
        assert s.pg_port == 9999
