"""Tests for the agency-audit package."""

import re
from pathlib import Path


def test_config_dsn():
    """Test that config DSN is constructed correctly with password."""
    from agency_audit.config import Settings

    s = Settings(
        pg_host="localhost",
        pg_port=5432,
        pg_user="testuser",
        pg_password="testpass",
        pg_database="testdb",
    )
    expected = "postgresql://testuser:***@localhost:5432/testdb"
    # Replace *** with actual password to match the real DSN
    expected = expected.replace("***", "testpass")
    assert s.dsn == expected


def test_config_dsn_no_password():
    """Test DSN without password."""
    from agency_audit.config import Settings

    s = Settings(
        pg_host="localhost",
        pg_port=5432,
        pg_user="testuser",
        pg_password="",
        pg_database="testdb",
    )
    assert s.dsn == "postgresql://testuser@localhost:5432/testdb"


def test_config_dsn_special_chars_in_password():
    """URL-encode special characters (@, :, /, %) in password."""

    from agency_audit.config import Settings

    password = "p@ss:word/with%chars"
    s = Settings(
        pg_host="localhost",
        pg_port=5432,
        pg_user="agency_audit",
        pg_password=password,
        pg_database="agency_audit",
    )
    # @ → %40, : → %3A, / → %2F, % → %25
    expected = "postgresql://agency_audit:p%40ss%3Aword%2Fwith%25chars@localhost:5432/agency_audit"
    assert s.dsn == expected


def test_config_dsn_special_chars_in_user():
    """URL-encode special characters (@, :, /, %) in username."""

    from agency_audit.config import Settings

    username = "us@r:n/me%"
    s = Settings(
        pg_host="localhost",
        pg_port=5432,
        pg_user=username,
        pg_password="plainpass",
        pg_database="testdb",
    )
    # @ → %40, : → %3A, / → %2F, % → %25
    expected = "postgresql://us%40r%3An%2Fme%25:plainpass@localhost:5432/testdb"
    assert s.dsn == expected


def test_country_count():
    """Verify exactly 44 countries in seed data."""
    seed_path = (
        Path(__file__).resolve().parents[1] / "src" / "agency_audit" / "seed" / "countries.sql"
    )
    sql = seed_path.read_text()
    matches = re.findall(r"\('([A-Z]{2})',\s*'([^']+)',\s*(?:true|false)\)", sql)
    assert len(matches) == 44, f"Expected 44 countries, got {len(matches)}"


def test_geonames_slugify():
    """Test slug generation from city names."""
    from agency_audit.geonames import _slugify

    assert _slugify("Sofia") == "sofia"
    assert _slugify("Veliko Turnovo") == "veliko-turnovo"
    assert _slugify("São Paulo") == "sao-paulo"
    assert _slugify("Düsseldorf") == "dusseldorf"
