"""Tests for the browser-scrape discovery helper.

All tests run against a live PostgreSQL database.  ``list_pending`` opens
its own connection via ``get_pool()``, so it requires the ``fresh_db``
fixture (private database with pool redirection) rather than ``db_conn``.
"""

from __future__ import annotations

import asyncpg

from agency_audit.scripts.discovery_helper import list_pending


async def test_list_pending_by_country(fresh_db: asyncpg.Connection) -> None:
    """Fetch pending cities for a specific country — only BG has seeded cities."""
    results = await list_pending(country_iso="BG", limit=3)
    assert len(results) == 3
    for r in results:
        assert r["country"] == "BG"
        assert "id" in r
        assert "label" in r
        assert "population" in r
        # latitude / longitude must NOT be exposed
        assert "latitude" not in r
        assert "longitude" not in r

    # Should be ordered by population DESC
    assert results[0]["population"] >= results[1]["population"]
    assert results[1]["population"] >= results[2]["population"]


async def test_list_pending_no_country(fresh_db: asyncpg.Connection) -> None:
    """Without a country filter, return top pending cities across all countries."""
    results = await list_pending(limit=5)
    assert len(results) == 5
    for r in results:
        assert "id" in r
        assert "label" in r
        assert "country" in r
        assert "population" in r
        assert "latitude" not in r
        assert "longitude" not in r

    # Should be ordered by population DESC
    for i in range(len(results) - 1):
        assert results[i]["population"] >= results[i + 1]["population"]


async def test_list_pending_country_no_pending(fresh_db: asyncpg.Connection) -> None:
    """A country with no pending cities returns an empty list."""
    results = await list_pending(country_iso="ZZ", limit=5)
    assert results == []
