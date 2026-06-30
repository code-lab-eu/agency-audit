"""Tests for DiscoveryPipeline.run_for_countries() and close() lifecycle.

Covers: run_for_countries contract, max_cities_per_country honoring,
discovery_status lifecycle, multi-country runs, and close delegation.
All tests mock httpx and the database pool — no live network or PostgreSQL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agency_audit.discovery import DiscoveryPipeline, PlaceResult, PlacesAPIClient

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_pool_mock():
    """Create a pool mock with configurable connection behaviour.

    Returns (mock_pool, mock_conn) where mock_conn is the shared
    connection that every pool.acquire() yields.  Callers set
    mock_conn.fetchrow.side_effect to control the sequence of
    returned rows.
    """
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=42)
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_conn
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire.return_value = mock_ctx

    mock_pool.fetch = AsyncMock(return_value=[])

    return mock_pool, mock_conn


def _make_place(place_id="pid1", name="Test Agency", website="https://test.example.com"):
    """Create a PlaceResult with sensible defaults."""
    return PlaceResult(
        place_id=place_id,
        name=name,
        formatted_address="123 Test St",
        phone="+359 2 123 4567",
        website=website,
        latitude=42.0,
        longitude=23.0,
        rating=4.5,
        user_ratings_total=42,
    )


def _make_city_row(
    city_id=1,
    label="Sofia",
    slug="sofia",
    country="BG",
    population=1236047,
    lat=42.69751,
    lon=23.32415,
):
    """Return a dict mimicking a cities table row."""
    return {
        "id": city_id,
        "label": label,
        "slug": slug,
        "population": population,
        "latitude": lat,
        "longitude": lon,
    }


# ──────────────────────────────────────────────────────────────────────
# run_for_countries — basic contract
# ──────────────────────────────────────────────────────────────────────


class TestRunForCountriesBasic:
    """Tests for the basic contract of run_for_countries()."""

    async def test_run_for_countries_single_country_one_city(self):
        """Single country, one city, two agencies found.

        fetchrow sequence:
          1. city selection  → _make_city_row()
          2. website lookup (pid1) → None  (new)
          3. website lookup (pid2) → None  (new)
          4. city selection  → None  (break)
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None, None, None]

        places = [
            _make_place("pid1", "Agency One"),
            _make_place("pid2", "Agency Two"),
        ]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=places)
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        # Contract: return structure
        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 1
        assert "BG" in result["results"]
        assert result["results"]["BG"] == {"cities": 1, "agencies": 2}

        # Contract: discovery_status set to 'done'
        done_calls = [
            c
            for c in conn_mock.execute.call_args_list
            if "discovery_status = 'done'" in str(c.args[0])
        ]
        assert len(done_calls) == 1

        # Contract: INSERT INTO discovery_log (searched)
        searched_calls = [
            c for c in conn_mock.execute.call_args_list if "'searched'" in str(c.args[0])
        ]
        assert len(searched_calls) == 1

    async def test_run_for_countries_returns_empty_when_no_cities(self):
        """When no pending cities, returns summary with zeros but country present."""
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [None]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock()
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
        assert result["countries_processed"] == 0
        # Country IS present in results even with zero cities
        assert "BG" in result["results"]
        assert result["results"]["BG"] == {"cities": 0, "agencies": 0}
        mock_places.search_text.assert_not_called()

    async def test_run_for_countries_no_agencies_found(self):
        """City processed, but Places API returns empty results.

        fetchrow: 1. city → city_row, 2. next city → None
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        assert result["results"]["BG"] == {"cities": 1, "agencies": 0}

        # discovery_status should still be set to 'done'
        done_calls = [
            c
            for c in conn_mock.execute.call_args_list
            if "discovery_status = 'done'" in str(c.args[0])
        ]
        assert len(done_calls) == 1


# ──────────────────────────────────────────────────────────────────────
# max_cities_per_country honoring
# ──────────────────────────────────────────────────────────────────────


class TestMaxCitiesHonoring:
    """Tests verifying max_cities_per_country is honored."""

    async def test_honors_max_cities_with_more_available(self):
        """max_cities_per_country=2, but 3+ cities pending — only 2 processed.

        fetchrow: 1.city1, 2.website1, 3.city2, 4.website2, 5.city3=None
        """
        city1 = _make_city_row(1, "Sofia", "sofia")
        city2 = _make_city_row(2, "Plovdiv", "plovdiv")
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [city1, None, city2, None, None]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=2,
            )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2  # 1 per city
        assert result["results"]["BG"] == {"cities": 2, "agencies": 2}

    async def test_fewer_cities_than_max(self):
        """max_cities_per_country=5 but only 2 cities available — 2 processed.

        fetchrow: 1.city1, 2.website1, 3.city2, 4.website2, 5.city3=None
        """
        city1 = _make_city_row(1, "Sofia", "sofia")
        city2 = _make_city_row(2, "Plovdiv", "plovdiv")
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [city1, None, city2, None, None]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=5,
            )

        assert result["cities_processed"] == 2


# ──────────────────────────────────────────────────────────────────────
# Multi-country runs
# ──────────────────────────────────────────────────────────────────────


class TestMultiCountry:
    """Tests for running discovery across multiple countries."""

    async def test_two_countries_each_one_city(self):
        """Two countries, one city each, one agency per city.

        fetchrow:
          1. city_BG    2. website_BG  3. next_BG=None
          4. city_RO    5. website_RO  6. next_RO=None
        """
        bg_city = _make_city_row(1, "Sofia", "sofia", "BG")
        ro_city = _make_city_row(2, "Bucuresti", "bucuresti", "RO", 1883425, 44.4268, 26.1025)
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [bg_city, None, None, ro_city, None, None]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG", "RO"],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2
        assert result["countries_processed"] == 2
        assert "BG" in result["results"]
        assert "RO" in result["results"]
        assert result["results"]["BG"] == {"cities": 1, "agencies": 1}
        assert result["results"]["RO"] == {"cities": 1, "agencies": 1}

    async def test_one_country_empty_one_with_cities(self):
        """BG has no pending cities, RO has one.

        fetchrow:
          1. BG city=None (break BG)
          2. RO city=ro_city  3. website=None  4. RO next=None
        """
        ro_city = _make_city_row(2, "Bucuresti", "bucuresti", "RO", 1883425, 44.4268, 26.1025)
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [None, ro_city, None, None]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG", "RO"],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        assert result["countries_processed"] == 1
        assert "RO" in result["results"]
        # BG always gets an entry, even with zero cities processed
        assert "BG" in result["results"]
        assert result["results"]["BG"] == {"cities": 0, "agencies": 0}
        assert result["results"]["RO"]["cities"] == 1


# ──────────────────────────────────────────────────────────────────────
# country_codes=None path (dynamic country detection)
# ──────────────────────────────────────────────────────────────────────


class TestDynamicCountryDetection:
    """Tests for run_for_countries when country_codes is None."""

    async def test_discovers_countries_from_db(self):
        """When country_codes=None, fetches pending countries from DB.

        pool.fetch returns BG + RO, then the inner loop fetches cities
        per country.  With max_cities_per_country=1, only the city fetch
        and website lookup are consumed per country — the while loop exits
        without a third fetch.
        """
        city_row = _make_city_row()
        pool_mock, _ = _make_pool_mock()
        pool_mock.fetch = AsyncMock(return_value=[{"country": "BG"}, {"country": "RO"}])

        # BG: city, website. RO: city, website.  No third fetch (while loop guard).
        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        city_ro = _make_city_row(3, "Bucuresti", "bucuresti", "RO")
        mock_conn.fetchrow.side_effect = [
            city_row,
            None,  # BG: city, website None (new)
            city_ro,
            None,  # RO: city, website None (new)
        ]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=None,
                max_cities_per_country=1,
            )

        pool_mock.fetch.assert_called_once()
        assert result["cities_processed"] == 2
        assert result["agencies_found"] == 2


# ──────────────────────────────────────────────────────────────────────
# Places API unavailable
# ──────────────────────────────────────────────────────────────────────


class TestPlacesUnavailable:
    """Tests for behavior when Places API is not available."""

    async def test_places_unavailable_zero_agencies(self):
        """When PlacesAPIClient.available is False, zero agencies found."""
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = False
        mock_places.search_text = AsyncMock()
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 0
        mock_places.search_text.assert_not_called()

        done_calls = [
            c
            for c in conn_mock.execute.call_args_list
            if "discovery_status = 'done'" in str(c.args[0])
        ]
        assert len(done_calls) == 1


# ──────────────────────────────────────────────────────────────────────
# DB writes verification
# ──────────────────────────────────────────────────────────────────────


class TestDBWrites:
    """Tests verifying the correct DB operations are performed."""

    async def test_writes_to_websites_table(self):
        """New agency triggers INSERT INTO websites via fetchval.

        fetchrow: 1.city, 2.website=None (new), 3.next_city=None
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None, None]
        conn_mock.fetchval = AsyncMock(return_value=42)

        place = _make_place(place_id="new-place", website="https://new.example.com")
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        # INSERT INTO websites should have been called via fetchval
        website_inserts = [
            c for c in conn_mock.fetchval.call_args_list if "INSERT INTO websites" in str(c.args[0])
        ]
        assert len(website_inserts) == 1

    async def test_writes_to_website_cities(self):
        """Each agency results in INSERT INTO website_cities.

        fetchrow: 1.city, 2.website_p1, 3.website_p2, 4.next=None
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None, None, None]

        places = [_make_place("p1", "A1"), _make_place("p2", "A2")]
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=places)
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        website_city_inserts = [
            c
            for c in conn_mock.execute.call_args_list
            if "INSERT INTO website_cities" in str(c.args[0])
        ]
        assert len(website_city_inserts) == 2

    async def test_writes_to_discovery_log(self):
        """Each agency + each city search writes to discovery_log.

        Total INSERTs: 2 'found' + 1 'searched' = 3.
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None, None, None]

        places = [_make_place("p1", "A1"), _make_place("p2", "A2")]
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=places)
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        discovery_inserts = [
            c
            for c in conn_mock.execute.call_args_list
            if "INSERT INTO discovery_log" in str(c.args[0])
        ]
        # 2 'found' + 1 'searched' = 3
        assert len(discovery_inserts) == 3

    async def test_existing_website_reused(self):
        """When website already exists, reuse its id (no duplicate INSERT).

        fetchrow: 1.city, 2.website={id:99} (exists), 3.next=None
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, {"id": 99}, None]
        conn_mock.fetchval = AsyncMock()  # should NOT be called for existing website

        place = _make_place(place_id="existing-place")
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        assert result["agencies_found"] == 1
        # fetchval (INSERT INTO websites) should NOT have been called
        website_inserts = [
            c for c in conn_mock.fetchval.call_args_list if "INSERT INTO websites" in str(c.args[0])
        ]
        assert len(website_inserts) == 0


# ──────────────────────────────────────────────────────────────────────
# close() lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestCloseLifecycle:
    """Tests for DiscoveryPipeline.close() lifecycle."""

    async def test_close_delegates_to_places_client(self):
        """close() calls places.close()."""
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)
        await pipeline.close()

        mock_places.close.assert_called_once()

    async def test_close_does_not_crash_when_places_is_none(self):
        """close() handles None places gracefully."""
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        pipeline.places = None
        # Should not raise
        await pipeline.close()

    async def test_run_and_close_lifecycle(self):
        """Full lifecycle: run_for_countries then close."""
        city_row = _make_city_row()
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [city_row, None, None]

        place = _make_place()
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[place])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1

        await pipeline.close()
        mock_places.close.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case tests for run_for_countries."""

    async def test_discovery_status_in_progress_set(self):
        """Before discover_city, city status is set to 'in_progress'."""
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=[])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        in_progress_calls = [
            c
            for c in conn_mock.execute.call_args_list
            if "discovery_status = 'in_progress'" in str(c.args[0])
        ]
        assert len(in_progress_calls) == 1

    async def test_empty_country_codes_list(self):
        """An empty country_codes list processes nothing — no DB queries."""
        pool_mock, _ = _make_pool_mock()

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock()
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=[],
                max_cities_per_country=3,
            )

        assert result["cities_processed"] == 0
        assert result["agencies_found"] == 0
        assert result["results"] == {}

    async def test_multiple_queries_per_city(self):
        """BG has 2 queries; with 25 first-query results, early stop at 20 places.

        fetchrow: 1.city, then 25 website lookups (only 20 counted due to early stop),
        then next city=None.
        Wait — discover_city discovers 25 places from search_text, filters dupes,
        early-stops the query loop at 20, reports at most 25. But we only give
        26 fetchrow side_effect values (1 city + 25 = 26, then None is never reached).
        Actually, the early stop in discover_city happens on the *query* loop (line 319),
        not the *fetchrow* loop. The fetchrow loop iterates over found_places which
        is 25 items after dedup. So we need 1 + 25 = 26 fetchrows then another for
        next city = 27. But with max_cities=3 and early-stop at 20, all 25 places
        are still found_places, so all 25 fetchrows happen.
        """
        city_row = _make_city_row()
        pool_mock, _ = _make_pool_mock()

        many_places = [_make_place(f"p{i}", f"Agency {i}") for i in range(25)]
        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        mock_places.search_text = AsyncMock(return_value=many_places)
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        # Provide enough fetchrow responses: 1 city + 25 website lookups + 1 next=None
        fetchrow_seq = [city_row] + [None] * 25 + [None]
        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = fetchrow_seq

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        # First query returns 25 results, triggers early stop (>=20)
        assert mock_places.search_text.call_count == 1
        assert result["agencies_found"] == 25

    async def test_search_text_error_handled(self):
        """When search_text raises, the error is caught and query loop continues.

        fetchrow: 1.city, then NO website fetchrows (both queries fail/empty), 2.next=None
        """
        city_row = _make_city_row()
        pool_mock, conn_mock = _make_pool_mock()
        conn_mock.fetchrow.side_effect = [city_row, None]

        mock_places = MagicMock(spec=PlacesAPIClient)
        mock_places.available = True
        # First query raises RuntimeError, second returns empty
        mock_places.search_text = AsyncMock(side_effect=[RuntimeError("API error"), []])
        mock_places.close = AsyncMock()

        pipeline = DiscoveryPipeline(places_client=mock_places)

        with patch("agency_audit.discovery.get_pool", return_value=pool_mock):
            result = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

        # Should not crash; city still marked done
        assert result["cities_processed"] == 1
        done_calls = [
            c
            for c in conn_mock.execute.call_args_list
            if "discovery_status = 'done'" in str(c.args[0])
        ]
        assert len(done_calls) == 1


# ──────────────────────────────────────────────────────────────────────
# CLI helper run_discovery
# ──────────────────────────────────────────────────────────────────────


class TestRunDiscovery:
    """Tests for the run_discovery CLI helper function."""

    async def test_run_discovery_with_key(self):
        """run_discovery with an available API key."""
        from agency_audit.discovery import run_discovery

        city_row = _make_city_row()
        pool_mock, _ = _make_pool_mock()

        mock_conn = pool_mock.acquire.return_value.__aenter__.return_value
        mock_conn.fetchrow.side_effect = [city_row, None, None]

        place = _make_place()
        with (
            patch("agency_audit.discovery.get_pool", return_value=pool_mock),
            patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls,
        ):
            mock_client = MagicMock(spec=PlacesAPIClient)
            mock_client.available = True
            mock_client.search_text = AsyncMock(return_value=[place])
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client

            result = await run_discovery(countries=["BG"], max_cities=1)

        assert result["cities_processed"] == 1
        assert result["agencies_found"] == 1
        mock_client.close.assert_called()

    async def test_run_discovery_no_api_key_raises(self):
        """run_discovery without an API key raises RuntimeError."""
        from agency_audit.discovery import run_discovery

        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            mock_client = MagicMock(spec=PlacesAPIClient)
            mock_client.available = False
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="No Google Maps API key"):
                await run_discovery(countries=["BG"])


# ──────────────────────────────────────────────────────────────────────
# DiscoveryPipeline lifecycle and pool methods (moved from
# test_geonames_discovery.py — uses mocked get_pool)
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelineMethods:
    """Close, pool creation, and cache methods for DiscoveryPipeline."""

    @pytest.mark.asyncio
    async def test_close(self):
        """close() delegates to the places client."""
        places = PlacesAPIClient(api_key="test")
        pipeline = DiscoveryPipeline(places_client=places)
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_close_no_places(self):
        """close() should not crash when places is None."""
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        pipeline.places = None
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_get_pool_creates_pool(self):
        """_get_pool calls get_pool() and caches the result."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool = await pipeline._get_pool()
            assert pool is not None
            mock_get_pool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pool_cached(self):
        """_get_pool returns the same pool on subsequent calls."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool1 = await pipeline._get_pool()
            pool2 = await pipeline._get_pool()
            assert pool1 is pool2
            mock_get_pool.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# DiscoveryPipeline.run_for_countries — full integration with mocks
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelineRunForCountries:
    """Tests for run_for_countries() with mocked pool, httpx, and places client.

    No live network or database required.
    """

    @staticmethod
    def _make_mock_pool():
        """Create a mock pool with acquire() returning an async context manager."""
        pool = MagicMock()
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__.return_value = conn
        pool.acquire.return_value = ctx
        return pool, conn

    @pytest.mark.asyncio
    async def test_run_for_countries_single_city_single_country(self):
        """run_for_countries returns correct summary for one city in one country."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # Mock city row for fetchrow
            conn.fetchrow = AsyncMock(
                return_value={
                    "id": 1,
                    "label": "Sofia",
                    "slug": "sofia",
                    "country": "BG",
                    "population": 1236047,
                    "latitude": 42.69751,
                    "longitude": 23.32415,
                }
            )
            # Mock website insert: no existing website, then return id=101
            conn.fetchval = AsyncMock(return_value=101)
            conn.execute = AsyncMock()

            # Mock places client to return one result
            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(
                return_value=[
                    PlaceResult(
                        place_id="abc123",
                        name="Agency One",
                        formatted_address="1 Main St",
                        phone="+359****3456",
                        website="https://agency1.bg",
                        latitude=42.69,
                        longitude=23.32,
                    )
                ]
            )

            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )

            # Assert return shape
            assert summary["countries_processed"] == 1
            assert summary["cities_processed"] == 1
            assert summary["agencies_found"] == 1
            assert "BG" in summary["results"]
            assert summary["results"]["BG"]["cities"] == 1
            assert summary["results"]["BG"]["agencies"] == 1

    @pytest.mark.asyncio
    async def test_run_for_countries_writes_to_websites_table(self):
        """discover_city writes to websites (via fetchval), website_cities, and discovery_log."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # First call: city row (run_for_countries). Second: no existing website (discover_city)
            conn.fetchrow = AsyncMock(
                side_effect=[
                    {
                        "id": 1,
                        "label": "Sofia",
                        "slug": "sofia",
                        "country": "BG",
                        "population": 1236047,
                        "latitude": 42.69751,
                        "longitude": 23.32415,
                    },
                    None,  # No existing website → INSERT path
                ]
            )
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(
                return_value=[
                    PlaceResult(
                        place_id="abc123",
                        name="Agency One",
                        website="https://agency1.bg",
                    )
                ]
            )

            pipeline = DiscoveryPipeline(places_client=places)
            await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

            # INSERT INTO websites goes through fetchval (uses RETURNING id)
            website_inserts = [
                c for c in conn.fetchval.call_args_list if "INSERT INTO websites" in str(c.args[0])
            ]
            assert len(website_inserts) == 1

            # website_cities and discovery_log writes go through execute
            execute_calls = [str(c) for c in conn.execute.call_args_list]
            all_sql = " ".join(execute_calls)
            assert "INSERT INTO website_cities" in all_sql
            assert "INSERT INTO discovery_log" in all_sql

    @pytest.mark.asyncio
    async def test_run_for_countries_sets_discovery_status_done(self):
        """After discover_city, discovery_status should be set to 'done'."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            conn.fetchrow = AsyncMock(
                return_value={
                    "id": 1,
                    "label": "Sofia",
                    "slug": "sofia",
                    "country": "BG",
                    "population": 1236047,
                    "latitude": 42.69751,
                    "longitude": 23.32415,
                }
            )
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(return_value=[])

            pipeline = DiscoveryPipeline(places_client=places)
            await pipeline.run_for_countries(country_codes=["BG"], max_cities_per_country=1)

            # Check that one of the execute calls sets discovery_status = 'done'
            done_calls = [
                str(c)
                for c in conn.execute.call_args_list
                if "discovery_status" in str(c) and "'done'" in str(c)
            ]
            assert len(done_calls) >= 1, (
                f"Expected at least one execute call with discovery_status='done', "
                f"got calls: {[str(c) for c in conn.execute.call_args_list]}"
            )

    @pytest.mark.asyncio
    async def test_run_for_countries_honors_max_cities_per_country(self):
        """Only max_cities_per_country cities should be processed per country."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # Return the same city row indefinitely so the while loop could run forever
            # but max_cities_per_country should stop it
            conn.fetchrow = AsyncMock(
                return_value={
                    "id": 1,
                    "label": "Sofia",
                    "slug": "sofia",
                    "country": "BG",
                    "population": 1236047,
                    "latitude": 42.69751,
                    "longitude": 23.32415,
                }
            )
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(return_value=[])

            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=2,
            )

            # Exactly 2 cities processed despite the same row being returned
            assert summary["cities_processed"] == 2
            assert summary["results"]["BG"]["cities"] == 2
            # fetchrow called once per city (while loop stops at max, no extra check)
            assert conn.fetchrow.call_count == 2

    @pytest.mark.asyncio
    async def test_run_for_countries_no_country_codes_auto_discovers(self):
        """When country_codes is None, discovers from DB via SELECT DISTINCT."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # pool.fetch returns countries
            pool.fetch = AsyncMock(return_value=[{"country": "BG"}, {"country": "RO"}])

            # fetchrow returns a city for each call; after max_cities returns None
            call_count = [0]

            async def fetchrow_side_effect(*args, **kwargs):
                call_count[0] += 1
                # Give each country 1 city, then None
                if call_count[0] <= 2:
                    return {
                        "id": call_count[0],
                        "label": f"City-{call_count[0]}",
                        "slug": f"city-{call_count[0]}",
                        "country": "BG" if call_count[0] == 1 else "RO",
                        "population": 100000,
                        "latitude": 42.0,
                        "longitude": 23.0,
                    }
                return None

            conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(return_value=[])

            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=None,
                max_cities_per_country=1,
            )

            # pool.fetch should have been called for auto-discovery
            pool.fetch.assert_called_once()
            assert summary["countries_processed"] == 2
            assert "BG" in summary["results"]
            assert "RO" in summary["results"]

    @pytest.mark.asyncio
    async def test_run_for_countries_empty_country_list(self):
        """Empty country_codes list is falsy → falls through to auto-discovery."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool
            # Since [] is falsy, the else branch calls pool.fetch — mock it
            pool.fetch = AsyncMock(return_value=[])

            places = PlacesAPIClient(api_key="test")
            pipeline = DiscoveryPipeline(places_client=places)

            summary = await pipeline.run_for_countries(
                country_codes=[],
                max_cities_per_country=3,
            )

            assert summary["countries_processed"] == 0
            assert summary["cities_processed"] == 0
            assert summary["agencies_found"] == 0
            assert summary["results"] == {}

    @pytest.mark.asyncio
    async def test_run_for_countries_no_pending_cities(self):
        """When no pending cities exist, summary reflects zero cities processed."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # First call returns None (no pending cities)
            conn.fetchrow = AsyncMock(return_value=None)
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            pipeline = DiscoveryPipeline(places_client=places)

            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

            assert summary["countries_processed"] == 0
            assert summary["cities_processed"] == 0
            assert summary["results"]["BG"]["cities"] == 0

    @pytest.mark.asyncio
    async def test_run_for_countries_existing_website_skipped_insert(self):
        """When a website already exists (maps_place_id match), UPDATE instead of INSERT."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            conn.fetchrow = AsyncMock(
                side_effect=[
                    # City row for run_for_countries fetch
                    {
                        "id": 1,
                        "label": "Sofia",
                        "slug": "sofia",
                        "country": "BG",
                        "population": 1236047,
                        "latitude": 42.69751,
                        "longitude": 23.32415,
                    },
                    # Existing website lookup: returns existing id
                    {"id": 99},
                ]
            )
            conn.fetchval = AsyncMock()  # Not called because existing found

            places = PlacesAPIClient(api_key="test")
            places.search_text = AsyncMock(
                return_value=[
                    PlaceResult(
                        place_id="existing123",
                        name="Existing Agency",
                        website="https://existing.bg",
                    )
                ]
            )

            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )

            assert summary["agencies_found"] == 1
            # fetchval should NOT have been called (we found existing website)
            conn.fetchval.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_lifecycle_closes_places_client(self):
        """close() calls places.close() which calls aclose on the HTTP client."""
        mock_http = AsyncMock()
        places = PlacesAPIClient(api_key="test")
        places._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places)
        await pipeline.close()

        mock_http.aclose.assert_called_once()
        assert places._client is None

    @pytest.mark.asyncio
    async def test_close_lifecycle_idempotent(self):
        """close() can be called multiple times safely."""
        mock_http = AsyncMock()
        places = PlacesAPIClient(api_key="test")
        places._client = mock_http

        pipeline = DiscoveryPipeline(places_client=places)
        await pipeline.close()
        await pipeline.close()  # Second call: places._client is None, no crash

        mock_http.aclose.assert_called_once()
        assert places._client is None

    @pytest.mark.asyncio
    async def test_run_for_countries_country_with_zero_cities(self):
        """Country with 0 cities processed should not increment countries_processed."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # No pending cities
            conn.fetchrow = AsyncMock(return_value=None)

            places = PlacesAPIClient(api_key="test")
            pipeline = DiscoveryPipeline(places_client=places)

            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=3,
            )

            assert summary["countries_processed"] == 0

    @pytest.mark.asyncio
    async def test_run_for_countries_places_error_handled_gracefully(self):
        """When places.search_text raises, discover_city continues without crashing."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            conn.fetchrow = AsyncMock(
                return_value={
                    "id": 1,
                    "label": "Sofia",
                    "slug": "sofia",
                    "country": "BG",
                    "population": 1236047,
                    "latitude": 42.69751,
                    "longitude": 23.32415,
                }
            )
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="test")
            # search_text raises an exception
            places.search_text = AsyncMock(side_effect=RuntimeError("API error"))

            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )

            # Should still complete and report 0 agencies
            assert summary["cities_processed"] == 1
            assert summary["agencies_found"] == 0

    @pytest.mark.asyncio
    async def test_run_for_countries_places_not_available_breaks(self):
        """When places.available is False, discover_city stops and reports 0."""
        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            conn.fetchrow = AsyncMock(
                return_value={
                    "id": 1,
                    "label": "Sofia",
                    "slug": "sofia",
                    "country": "BG",
                    "population": 1236047,
                    "latitude": 42.69751,
                    "longitude": 23.32415,
                }
            )
            conn.fetchval = AsyncMock(return_value=42)

            places = PlacesAPIClient(api_key="")  # Empty key → not available
            pipeline = DiscoveryPipeline(places_client=places)
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )

            assert summary["cities_processed"] == 1
            assert summary["agencies_found"] == 0
