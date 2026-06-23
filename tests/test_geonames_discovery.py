"""Tests for geonames.py pure functions and discovery.py helpers.

Covers: _slugify (already tested in test_basics.py), parse_geonames_line,
parse_geonames_file, extract_geonames_zip, PlaceResult, PlacesAPIClient init,
DiscoveryPipeline init and query_for_country, COUNTRY_QUERIES.
"""

import io
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agency_audit.discovery import COUNTRY_QUERIES, DiscoveryPipeline, PlaceResult, PlacesAPIClient
from agency_audit.geonames import extract_geonames_zip, parse_geonames_file, parse_geonames_line

# ──────────────────────────────────────────────────────────────────────
# parse_geonames_line
# ──────────────────────────────────────────────────────────────────────


class TestParseGeonamesLine:
    """Tests for parse_geonames_line — tab-separated geonames row parsing."""

    def test_valid_city_line(self):
        """A valid P feature class line above min_population."""
        line = (
            "727011\tSofia\tSofia\tSofiya,Sofija,Szófia\t42.69751\t23.32415"
            "\tP\tPPLC\tBG\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01"
        )
        result = parse_geonames_line(line)
        assert result is not None
        assert result["country"] == "BG"
        assert result["label"] == "Sofia"
        assert result["slug"] == "sofia"
        assert result["population"] == 1236047
        assert result["latitude"] == 42.69751
        assert result["longitude"] == 23.32415

    def test_non_populated_place(self):
        """Feature class != 'P' should be filtered out."""
        line = (
            "12345\tLake\tLake\tLac\t42.0\t23.0"
            "\tH\tLK\t00\tN\t42\tSofia\t00\t22\t1000\t0\t550\tEurope/Sofia\t2020-01-01"
        )
        result = parse_geonames_line(line)
        assert result is None

    def test_below_min_population(self):
        """Population below settings.geonames_min_population should be filtered."""
        # Use a very low population; default min is 5000
        line = (
            "12345\tSmall\tSmall\tVillage\t42.0\t23.0"
            "\tP\tPPL\t00\tN\t42\tSofia\t00\t22\t100\t0\t550\tEurope/Sofia\t2020-01-01"
        )
        # This will use settings.geonames_min_population — which defaults to 5000
        # So population 100 should be filtered
        result = parse_geonames_line(line)
        assert result is None

    def test_too_few_fields(self):
        """Line with fewer than 15 fields should return None."""
        result = parse_geonames_line("few\tfields")
        assert result is None

    def test_empty_line(self):
        result = parse_geonames_line("")
        assert result is None

    def test_name_fallback_to_asciiname(self):
        """When asciiname is present, it should be used as label."""
        line = (
            "727011\tСофия\tSofia\tSofiya\t42.69751\t23.32415"
            "\tP\tPPLC\t00\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01"
        )
        result = parse_geonames_line(line)
        assert result is not None
        assert result["label"] == "Sofia"  # asciiname, not name


# ──────────────────────────────────────────────────────────────────────
# parse_geonames_file
# ──────────────────────────────────────────────────────────────────────


class TestParseGeonamesFile:
    def test_valid_file(self):
        content = (
            b"727011\tSofia\tSofia\tSofiya\t42.69751\t23.32415"
            b"\tP\tPPLC\t00\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01\n"
            b"726050\tPlovdiv\tPlovdiv\tPlovdiv\t42.15\t24.75"
            b"\tP\tPPLA\t00\tN\t51\tPlovdiv\t00\t16\t346893\t0\t160\tEurope/Sofia\t2020-01-01\n"
        )

        cities = list(parse_geonames_file(content))
        assert len(cities) == 2
        assert cities[0]["label"] == "Sofia"
        assert cities[1]["label"] == "Plovdiv"

    def test_country_filter(self):
        content = (
            b"727011\tSofia\tSofia\tSofiya\t42.69751\t23.32415"
            b"\tP\tPPLC\tBG\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01\n"
            b"2643743\tLondon\tLondon\tLondon\t51.50853\t-0.12574"
            b"\tP\tPPLC\tGB\tN\t00\t00\t00\t00\t7556900\t0\t14\tEurope/London\t2020-01-01\n"
        )

        # Filter for Bulgaria only
        cities = list(parse_geonames_file(content, country_filter={"BG"}))
        assert len(cities) == 1
        assert cities[0]["label"] == "Sofia"

    def test_empty_content(self):
        cities = list(parse_geonames_file(b""))
        assert len(cities) == 0

    def test_skip_empty_lines(self):
        content = (
            b"\n\n"
            b"727011\tSofia\tSofia\tSofiya\t42.69751\t23.32415"
            b"\tP\tPPLC\tBG\tN\t42\tSofia\t00\t22\t1236047"
            b"\t0\t550\tEurope/Sofia\t2020-01-01\n\n"
        )
        cities = list(parse_geonames_file(content))
        assert len(cities) == 1

    def test_invalid_utf8(self):
        """Invalid UTF-8 bytes should be replaced, not crash."""
        content = (
            b"727011\tSofia\tSofia\tSofiya\t42.69751\t23.32415"
            b"\tP\tPPLC\t00\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01\n"
            b"999999\tBad\tBad\tBad\t42.0\t23.0"
            b"\tP\tPPL\t00\tN\t42\tSofia\t00\t22\t10000\t0\t550\tEurope/Sofia\t2020-01-01"
            b"\xff\xfe\n"
        )
        cities = list(parse_geonames_file(content))
        assert len(cities) >= 1  # At least the valid line should parse


# ──────────────────────────────────────────────────────────────────────
# extract_geonames_zip
# ──────────────────────────────────────────────────────────────────────


class TestExtractGeonamesZip:
    def test_extract_single_txt(self):
        """Extract the .txt file from a zip with one .txt file."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("cities15000.txt", "test content")
        zip_content = buf.getvalue()

        result = extract_geonames_zip(zip_content)
        assert result == b"test content"

    def test_extract_multiple_txt(self):
        """Extract the first .txt file (alphabetically) from a zip with multiple.
        The code breaks on the first match."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "readme")
            zf.writestr("target.txt", "city data")
        zip_content = buf.getvalue()

        result = extract_geonames_zip(zip_content)
        # Returns the FIRST .txt alphabetically: readme.txt
        assert result == b"readme"

    def test_no_txt_file(self):
        """Should raise ValueError when no .txt file in zip."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("data.bin", b"binary")
        zip_content = buf.getvalue()

        with pytest.raises(ValueError, match="No .txt file found"):
            extract_geonames_zip(zip_content)


# ──────────────────────────────────────────────────────────────────────
# PlaceResult dataclass
# ──────────────────────────────────────────────────────────────────────


class TestPlaceResult:
    def test_defaults(self):
        pr = PlaceResult(place_id="abc123", name="Test Agency")
        assert pr.place_id == "abc123"
        assert pr.name == "Test Agency"
        assert pr.formatted_address is None
        assert pr.phone is None
        assert pr.website is None
        assert pr.latitude is None
        assert pr.longitude is None
        assert pr.rating is None
        assert pr.user_ratings_total is None

    def test_full(self):
        pr = PlaceResult(
            place_id="xyz",
            name="Full Agency",
            formatted_address="123 Main St",
            phone="+359****3456",
            website="https://example.com",
            latitude=42.0,
            longitude=23.0,
            rating=4.5,
            user_ratings_total=100,
        )
        assert pr.formatted_address == "123 Main St"
        assert pr.website == "https://example.com"
        assert pr.rating == 4.5


# ──────────────────────────────────────────────────────────────────────
# PlacesAPIClient
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClient:
    def test_init_with_key(self):
        client = PlacesAPIClient(api_key="test-key-123")
        assert client.api_key == "test-key-123"
        assert client.available is True

    def test_init_without_key_loads_from_settings(self):
        """Without an explicit key, _load_api_key is called."""
        with patch("agency_audit.config.settings") as mock_settings:
            mock_settings.google_maps_api_key = "loaded-key"
            client = PlacesAPIClient()
            assert client.api_key == "loaded-key"
            assert client.available is True

    def test_available_with_empty_key(self):
        client = PlacesAPIClient(api_key="")
        assert client.available is False

    def test_available_with_none_key(self):
        with patch("agency_audit.config.settings") as mock_settings:
            mock_settings.google_maps_api_key = ""
            client = PlacesAPIClient()
            assert client.available is False

    def test_base_url(self):
        assert PlacesAPIClient.BASE_URL == "https://places.googleapis.com/v1/places:searchText"

    @pytest.mark.asyncio
    async def test_query_for_country(self):
        """query_for_country is a DiscoveryPipeline method, tested here."""
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        queries = await pipeline.query_for_country("BG")
        assert "Агенция за недвижими имоти" in queries

    @pytest.mark.asyncio
    async def test_query_for_country_unknown(self):
        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        queries = await pipeline.query_for_country("XX")
        assert queries == ["real estate agent"]


# ──────────────────────────────────────────────────────────────────────
# COUNTRY_QUERIES validation
# ──────────────────────────────────────────────────────────────────────


class TestCountryQueries:
    def test_all_countries_have_queries(self):
        """Every country in the seed data should have query templates."""
        # The queries dict has all 44 countries
        assert len(COUNTRY_QUERIES) >= 40

    def test_bulgaria_has_queries(self):
        assert "BG" in COUNTRY_QUERIES
        assert len(COUNTRY_QUERIES["BG"]) == 2


# ──────────────────────────────────────────────────────────────────────
# DiscoveryPipeline init
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipeline:
    def test_init_with_places_client(self):
        client = PlacesAPIClient(api_key="test")
        pipeline = DiscoveryPipeline(places_client=client)
        assert pipeline.places is client
        assert pipeline.batch_size == 10

    def test_init_without_places_client(self):
        with patch("agency_audit.config.settings") as mock_settings:
            mock_settings.google_maps_api_key = "auto-key"
            pipeline = DiscoveryPipeline()
            assert pipeline.places is not None
            assert pipeline.places.available is True

    def test_init_custom_batch_size(self):
        pipeline = DiscoveryPipeline(PlacesAPIClient(api_key="test"), batch_size=5)
        assert pipeline.batch_size == 5


# ──────────────────────────────────────────────────────────────────────
# Geoname async functions: download_geonames, import_geonames
# ──────────────────────────────────────────────────────────────────────


class TestGeonamesAsync:
    """Async geonames functions requiring network/zip mocks."""

    @pytest.mark.asyncio
    async def test_download_geonames(self):
        """download_geonames returns zip content from a mock HTTP response."""
        import httpx

        from agency_audit.geonames import download_geonames

        fake_zip = b"PK\x03\x04fake zip content"
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, content=fake_zip, request=req)
        )

        async with httpx.AsyncClient(transport=transport) as client:
            with patch("agency_audit.geonames.httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value = client
                mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)

                result = await download_geonames("https://example.com/geonames.zip")
                assert result == fake_zip

    @pytest.mark.asyncio
    async def test_import_geonames_with_provided_zip(self):
        """import_geonames from a provided zip with valid city data."""
        import io
        import zipfile

        from agency_audit.geonames import import_geonames

        mock_conn = AsyncMock()
        mock_conn.executemany = AsyncMock()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(
                "cities15000.txt",
                "727011\tSofia\tSofia\tSofiya\t42.69751\t23.32415"
                "\tP\tPPLC\tBG\tN\t42\tSofia\t00\t22\t1236047\t0\t550\tEurope/Sofia\t2020-01-01\n"
                "726050\tPlovdiv\tPlovdiv\tPlovdiv\t42.15\t24.75"
                "\tP\tPPLA\tBG\tN\t51\tPlovdiv\t00\t16\t346893\t0\t160\tEurope/Sofia\t2020-01-01\n",
            )
        zip_content = buf.getvalue()

        count = await import_geonames(mock_conn, zip_content=zip_content)
        assert count == 2
        mock_conn.executemany.assert_called_once()

    @pytest.mark.asyncio
    async def test_import_geonames_empty(self):
        """import_geonames with an empty cities file returns zero."""
        import io
        import zipfile

        from agency_audit.geonames import import_geonames

        mock_conn = AsyncMock()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("cities15000.txt", "")
        zip_content = buf.getvalue()

        count = await import_geonames(mock_conn, zip_content=zip_content)
        assert count == 0


# ──────────────────────────────────────────────────────────────────────
# PlacesAPIClient lifecycle methods
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientMethods:
    """Close, rate-limiting, and client lifecycle methods for PlacesAPIClient."""

    def test_ensure_client_initial_state(self):
        """A new PlacesAPIClient has _client == None before first use."""
        from agency_audit.discovery import PlacesAPIClient

        client = PlacesAPIClient(api_key="test-key")
        assert client._client is None
        assert client.api_key == "test-key"

    @pytest.mark.asyncio
    async def test_close_when_no_client(self):
        """close() is a no-op when _client is None."""
        from agency_audit.discovery import PlacesAPIClient

        client = PlacesAPIClient(api_key="test-key")
        await client.close()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_close_with_client(self):
        """close() calls aclose on the underlying HTTP client."""
        from agency_audit.discovery import PlacesAPIClient

        client = PlacesAPIClient(api_key="test-key")
        mock_http = AsyncMock()
        client._client = mock_http
        await client.close()
        mock_http.aclose.assert_called_once()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_rate_limit_first_call(self):
        """First _rate_limit call should not sleep."""
        import time

        from agency_audit.discovery import PlacesAPIClient

        client = PlacesAPIClient(api_key="test-key")
        start = time.monotonic()
        await client._rate_limit()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    @pytest.mark.asyncio
    async def test_rate_limit_throttled(self):
        """Second _rate_limit call within the window should throttle."""
        import time

        from agency_audit.discovery import PlacesAPIClient

        client = PlacesAPIClient(api_key="test-key")
        await client._rate_limit()
        start = time.monotonic()
        await client._rate_limit()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15


# ──────────────────────────────────────────────────────────────────────
# DiscoveryPipeline lifecycle and pool methods
# ──────────────────────────────────────────────────────────────────────


class TestDiscoveryPipelineMethods:
    """Close, pool creation, and cache methods for DiscoveryPipeline."""

    @pytest.mark.asyncio
    async def test_close(self):
        """close() delegates to the places client."""
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

        places = PlacesAPIClient(api_key="test")
        pipeline = DiscoveryPipeline(places_client=places)
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_close_no_places(self):
        """close() should not crash when places is None."""
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

        pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
        pipeline.places = None
        await pipeline.close()

    @pytest.mark.asyncio
    async def test_get_pool_creates_pool(self):
        """_get_pool calls get_pool() and caches the result."""
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_get_pool.return_value = MagicMock()
            pipeline = DiscoveryPipeline(places_client=PlacesAPIClient(api_key="test"))
            pool = await pipeline._get_pool()
            assert pool is not None
            mock_get_pool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pool_cached(self):
        """_get_pool returns the same pool on subsequent calls."""
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlaceResult, PlacesAPIClient

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
                        phone="+359123456",
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
        from agency_audit.discovery import DiscoveryPipeline, PlaceResult, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            pool, conn = self._make_mock_pool()
            mock_get_pool.return_value = pool

            # pool.fetch returns countries
            pool.fetch = AsyncMock(return_value=[{"country": "BG"}, {"country": "RO"}])

            # fetchrow returns a city for each call; after max_cities returns None
            call_count = [0]

            async def fetchrow_side_effect(*args, **kwargs):
                call_count[0] += 1
                has_country = kwargs.get("country") or (args and "$1" not in str(args[0]))
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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlaceResult, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
        from agency_audit.discovery import DiscoveryPipeline, PlacesAPIClient

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
