"""Tests for the PlacesAPIClient in discovery.py.

Covers: API-key resolution, request construction, Response -> PlaceResult
mapping, rate-limit behaviour, and error paths.  No live network or DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agency_audit.discovery import PlaceResult, PlacesAPIClient

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_place_payload():
    """A single complete Google Places API place response object."""
    return {
        "id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
        "displayName": {"text": "Агенция Имоти", "languageCode": "bg"},
        "formattedAddress": "ул. Граф Игнатиев 18, София 1000",
        "internationalPhoneNumber": "+359 2 123 4567",
        "websiteUri": "https://example-agency.bg",
        "location": {"latitude": 42.6977, "longitude": 23.3219},
        "rating": 4.6,
        "userRatingCount": 142,
    }


@pytest.fixture
def sample_response(sample_place_payload):
    """A complete API response with one place and no next page."""
    return {"places": [sample_place_payload]}


@pytest.fixture
def sample_response_paginated(sample_place_payload):
    """First page of a paginated response, with a nextPageToken."""
    return {
        "places": [sample_place_payload],
        "nextPageToken": "CkQ_abc123",
    }


# ──────────────────────────────────────────────────────────────────────
# 1. API-key resolution
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientInit:
    """API-key sourcing and the ``available`` property."""

    def test_explicit_key_sets_api_key(self):
        """Explicit api_key is stored directly; available reports True."""
        client = PlacesAPIClient(api_key="explicit-key-123")
        assert client.api_key == "explicit-key-123"
        assert client.available is True

    def test_no_key_loads_from_settings(self):
        """Without an explicit key, _load_api_key reads from config."""
        with patch("agency_audit.config.settings.google_maps_api_key", "config-key-456"):
            client = PlacesAPIClient()
            assert client.api_key == "config-key-456"

    def test_empty_key_not_available(self):
        """When the API key is empty, available must be False."""
        client = PlacesAPIClient(api_key="")
        assert client.available is False

    def test_none_key_not_available_when_config_empty(self):
        """When no explicit key and config has empty string, available is False."""
        with patch("agency_audit.config.settings.google_maps_api_key", ""):
            client = PlacesAPIClient()
            assert client.available is False

    def test_explicit_none_falls_back_to_config(self):
        """Passing None explicitly falls back to config (same as default)."""
        with patch("agency_audit.config.settings.google_maps_api_key", "from-config-789"):
            client = PlacesAPIClient(api_key=None)
            assert client.api_key == "from-config-789"


# ──────────────────────────────────────────────────────────────────────
# 2. Request construction
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientRequestConstruction:
    """Verify the HTTP request body, headers, and URL."""

    @pytest.mark.asyncio
    async def test_basic_request_body(self, sample_response):
        """search_text sends the correct JSON body without location bias."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = sample_response
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        await client.search_text(query="и недвижими имоти")

        call_args = mock_client.post.call_args
        assert call_args[0][0] == PlacesAPIClient.BASE_URL

        body = call_args[1]["json"]
        assert body["textQuery"] == "и недвижими имоти"
        assert body["pageSize"] == 20
        assert "locationBias" not in body

    @pytest.mark.asyncio
    async def test_request_with_location_bias(self, sample_response):
        """location_bias tuple is serialised into a circle restriction."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = sample_response
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        await client.search_text(
            query="estate agent", location_bias=(51.5074, -0.1278), radius=5000
        )

        body = mock_client.post.call_args[1]["json"]
        assert body["textQuery"] == "estate agent"
        assert body["locationBias"] == {
            "circle": {
                "center": {"latitude": 51.5074, "longitude": -0.1278},
                "radius": 5000,
            }
        }

    @pytest.mark.asyncio
    async def test_headers_set_on_client(self, sample_response):
        """The AsyncClient is configured with the correct headers."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = sample_response
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="header-test-key")
        client._client = mock_client

        await client.search_text(query="immobilienmakler")

        post_headers = mock_client.post.call_args[1].get("headers")
        assert post_headers is None  # headers live on the client, not per-request

        # The client itself should have been created with proper headers.
        # We can verify by checking __init__ helpers.
        with patch("agency_audit.discovery.asyncio.sleep", new_callable=AsyncMock):
            client2 = PlacesAPIClient(api_key="header-test-key")
            await client2._ensure_client()
            assert client2._client.headers["X-Goog-Api-Key"] == "header-test-key"
            assert client2._client.headers["Content-Type"] == "application/json"
            assert "X-Goog-FieldMask" in client2._client.headers
            await client2.close()

    @pytest.mark.asyncio
    async def test_page_token_in_request(self, sample_response):
        """When nextPageToken is present, the second request includes it."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        page1_response = MagicMock(spec=httpx.Response)
        page1_response.status_code = 200
        page1_response.json.return_value = {
            "places": [
                {
                    "id": "p1",
                    "displayName": {"text": "Agency One"},
                    "formattedAddress": "Addr 1",
                    "location": {"latitude": 1.0, "longitude": 1.0},
                }
            ],
            "nextPageToken": "token-xyz",
        }

        page2_response = MagicMock(spec=httpx.Response)
        page2_response.status_code = 200
        page2_response.json.return_value = {
            "places": [
                {
                    "id": "p2",
                    "displayName": {"text": "Agency Two"},
                    "formattedAddress": "Addr 2",
                    "location": {"latitude": 2.0, "longitude": 2.0},
                }
            ],
        }

        mock_client.post.side_effect = [page1_response, page2_response]

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test", max_results=40)

        assert len(results) == 2
        # Second call should include pageToken
        second_body = mock_client.post.call_args_list[1][1]["json"]
        assert second_body["pageToken"] == "token-xyz"

    @pytest.mark.asyncio
    async def test_page_size_clamped(self, sample_response):
        """pageSize is min(20, remaining) — respects the remaining slot count."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        page_response = MagicMock(spec=httpx.Response)
        page_response.status_code = 200
        page_response.json.return_value = {
            "places": [
                {
                    "id": f"p{i}",
                    "displayName": {"text": f"Agency {i}"},
                    "location": {"latitude": float(i), "longitude": float(i)},
                }
                for i in range(3)
            ],
        }
        mock_client.post.return_value = page_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        # Only 3 results remaining, pageSize should be 3
        await client.search_text(query="test", max_results=3)

        body = mock_client.post.call_args[1]["json"]
        assert body["pageSize"] == 3


# ──────────────────────────────────────────────────────────────────────
# 3. Response → PlaceResult mapping
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientResponseMapping:
    """API JSON is correctly transformed into PlaceResult objects."""

    @pytest.mark.asyncio
    async def test_full_field_mapping(self, sample_place_payload):
        """Every PlaceResult field maps to the correct JSON path."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"places": [sample_place_payload]}
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")

        assert len(results) == 1
        p = results[0]
        assert isinstance(p, PlaceResult)
        assert p.place_id == "ChIJN1t_tDeuEmsRUsoyG83frY4"
        assert p.name == "Агенция Имоти"
        assert p.formatted_address == "ул. Граф Игнатиев 18, София 1000"
        assert p.phone == "+359 2 123 4567"
        assert p.website == "https://example-agency.bg"
        assert p.latitude == 42.6977
        assert p.longitude == 23.3219
        assert p.rating == 4.6
        assert p.user_ratings_total == 142

    @pytest.mark.asyncio
    async def test_minimal_place(self):
        """Place with only id and displayName is handled gracefully."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "places": [
                {
                    "id": "minimal-id",
                    "displayName": {"text": "Minimal Agency"},
                }
            ],
        }
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")

        assert len(results) == 1
        p = results[0]
        assert p.place_id == "minimal-id"
        assert p.name == "Minimal Agency"
        assert p.formatted_address is None
        assert p.phone is None
        assert p.website is None
        assert p.latitude is None
        assert p.longitude is None
        assert p.rating is None
        assert p.user_ratings_total is None

    @pytest.mark.asyncio
    async def test_empty_places_list(self):
        """API response with no places returns an empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"places": []}
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")
        assert results == []

    @pytest.mark.asyncio
    async def test_missing_display_name_text(self):
        """Place with displayName but no text key defaults to empty string."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "places": [
                {
                    "id": "no-name-id",
                    "displayName": {},
                }
            ],
        }
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")
        assert len(results) == 1
        assert results[0].name == ""

    @pytest.mark.asyncio
    async def test_multiple_places(self):
        """A response with multiple places returns all of them."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "places": [
                {
                    "id": f"id-{i}",
                    "displayName": {"text": f"Agency {i}"},
                    "location": {"latitude": float(i), "longitude": float(i)},
                }
                for i in range(5)
            ],
        }
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")
        assert len(results) == 5
        assert all(isinstance(p, PlaceResult) for p in results)
        assert [p.place_id for p in results] == [
            "id-0",
            "id-1",
            "id-2",
            "id-3",
            "id-4",
        ]


# ──────────────────────────────────────────────────────────────────────
# 4. Rate limiting
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientRateLimit:
    """The internal _rate_limit enforces the 5 QPS throttle."""

    @pytest.mark.asyncio
    async def test_rate_limit_calls_sleep_when_interval_too_short(self):
        """When called quickly back-to-back, it sleeps to enforce min interval."""
        client = PlacesAPIClient(api_key="test-key")

        with patch("agency_audit.discovery.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Simulate a call that finishes, then wait 0.05s, then call again
            client._last_request_time = 100.0
            # time.monotonic() returns 100.05 for the second call
            with patch("agency_audit.discovery.time.monotonic", return_value=100.05):
                await client._rate_limit()
                # Should have slept 0.2 - 0.05 = 0.15 seconds
                mock_sleep.assert_called_once()
                assert mock_sleep.call_args[0][0] == pytest.approx(0.15)
                assert client._request_count == 1

    @pytest.mark.asyncio
    async def test_no_sleep_when_interval_exceeded(self):
        """When the interval already exceeds the min, no sleep is called."""
        client = PlacesAPIClient(api_key="test-key")

        with patch("agency_audit.discovery.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            client._last_request_time = 100.0
            # Enough time has passed (1.0s > 0.2s min)
            with patch("agency_audit.discovery.time.monotonic", return_value=101.0):
                await client._rate_limit()
                mock_sleep.assert_not_called()
                assert client._request_count == 1


# ──────────────────────────────────────────────────────────────────────
# 5. Error-path handling
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientErrorHandling:
    """Error and edge-case paths during search_text."""

    @pytest.mark.asyncio
    async def test_http_status_error_raised(self):
        """HTTP 4xx/5xx responses raise HTTPStatusError."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=mock_response
        )

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        with pytest.raises(httpx.HTTPStatusError, match="403 Forbidden"):
            await client.search_text(query="test")

    @pytest.mark.asyncio
    async def test_timeout_breaks_loop_returns_partial(self):
        """A timeout exception breaks out of the loop, preserving any results."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # First page succeeds
        page1 = MagicMock(spec=httpx.Response)
        page1.status_code = 200
        page1.json.return_value = {
            "places": [
                {
                    "id": "p1",
                    "displayName": {"text": "Found Agency"},
                    "location": {"latitude": 1.0, "longitude": 1.0},
                }
            ],
            "nextPageToken": "token-1",
        }
        mock_client.post.side_effect = [page1, httpx.TimeoutException("timed out")]

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test", max_results=40)
        assert len(results) == 1
        assert results[0].place_id == "p1"

    @pytest.mark.asyncio
    async def test_missing_places_key_returns_empty(self):
        """Response without a 'places' key yields an empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test")
        assert results == []

    @pytest.mark.asyncio
    async def test_results_truncated_to_max(self):
        """When the API returns more than max_results, output is truncated."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "places": [
                {
                    "id": f"id-{i}",
                    "displayName": {"text": f"Agency {i}"},
                    "location": {"latitude": float(i), "longitude": float(i)},
                }
                for i in range(25)
            ],
        }
        mock_client.post.return_value = mock_response

        client = PlacesAPIClient(api_key="test-key")
        client._client = mock_client

        results = await client.search_text(query="test", max_results=10)
        assert len(results) == 10


# ──────────────────────────────────────────────────────────────────────
# 6. Client lifecycle
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientLifecycle:
    """Client initialisation, close, and _ensure_client behaviour."""

    @pytest.mark.asyncio
    async def test_ensure_client_creates_once(self):
        """Repeated calls to _ensure_client return the same instance."""
        client = PlacesAPIClient(api_key="lifecycle-key")
        c1 = await client._ensure_client()
        c2 = await client._ensure_client()
        assert c1 is c2
        await client.close()

    @pytest.mark.asyncio
    async def test_close_cleans_up_client(self):
        """close() tears down the httpx client and sets _client to None."""
        client = PlacesAPIClient(api_key="lifecycle-key")
        await client._ensure_client()
        assert client._client is not None
        await client.close()
        assert client._client is None


# =========================================================================
# run_discovery wrapper, DiscoveryPipeline, and CLI help text tests
# =========================================================================


class TestRunDiscovery:
    """Tests for the run_discovery wrapper function."""

    async def test_no_api_key_raises_runtime_error(self):
        """run_discovery raises RuntimeError when GOOGLE_MAPS_API_KEY is unset."""
        from agency_audit.discovery import run_discovery

        with patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.available = False
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="No Google Maps API key found"):
                await run_discovery(countries=["BG"], max_cities=1)

    async def test_available_api_key_runs_pipeline(self):
        """run_discovery runs the pipeline when API key is set."""
        from agency_audit.discovery import run_discovery

        with (
            patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls,
            patch("agency_audit.discovery.get_pool") as mock_get_pool,
        ):
            mock_client = MagicMock()
            mock_client.available = True
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client

            # Mock pool: no pending cities -> fetchrow returns None -> loop breaks
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchrow = AsyncMock(return_value=None)

            summary = await run_discovery(countries=["BG"], max_cities=1)
            assert summary["countries_processed"] == 0
            assert summary["cities_processed"] == 0
            assert summary["agencies_found"] == 0

    async def test_closes_pipeline_on_error(self):
        """run_discovery closes the pipeline even when an error occurs."""
        from agency_audit.discovery import run_discovery

        with (
            patch("agency_audit.discovery.PlacesAPIClient") as mock_client_cls,
            patch("agency_audit.discovery.get_pool") as mock_get_pool,
        ):
            mock_client = MagicMock()
            mock_client.available = True
            mock_client.close = AsyncMock()
            mock_client_cls.return_value = mock_client

            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            # Raise on pool.fetch() -- pass no countries so fetch() is called
            mock_pool.fetch = AsyncMock(side_effect=RuntimeError("DB failure"))

            with pytest.raises(RuntimeError, match="DB failure"):
                await run_discovery(max_cities=1)

            # Pipeline.close should have been called (via finally)
            mock_client.close.assert_called_once()


class TestDiscoveryPipelineIntegration:
    """Tests for the DiscoveryPipeline orchestrator integration."""

    async def test_query_for_country_known(self):
        """query_for_country returns the correct queries for a known country."""
        from agency_audit.discovery import DiscoveryPipeline

        pipeline = DiscoveryPipeline()
        queries = await pipeline.query_for_country("BG")
        assert len(queries) >= 2
        assert "имоти" in queries[0].lower()

    async def test_query_for_country_unknown(self):
        """query_for_country returns a fallback for unknown countries."""
        from agency_audit.discovery import DiscoveryPipeline

        pipeline = DiscoveryPipeline()
        queries = await pipeline.query_for_country("XX")
        assert queries == ["real estate agent"]

    async def test_discover_city_no_api_key_logs_warning(self):
        """discover_city logs a warning and returns 0 when API is unavailable."""
        from agency_audit.discovery import DiscoveryPipeline

        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchrow = AsyncMock(return_value=None)

            pipeline = DiscoveryPipeline()
            with (
                patch.object(
                    type(pipeline.places),
                    "available",
                    new_callable=lambda: property(lambda self: False),
                ),
                patch("agency_audit.discovery.logger") as mock_logger,
            ):
                result = await pipeline.discover_city(
                    city_id=1,
                    city_label="Sofia",
                    city_slug="sofia",
                    country_iso="BG",
                    latitude=42.7,
                    longitude=23.3,
                )
                assert result == 0
                mock_logger.warning.assert_called_once()

    async def test_run_for_countries_no_cities(self):
        """run_for_countries returns empty summary when no pending cities."""
        from agency_audit.discovery import DiscoveryPipeline

        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            # Set up pool.acquire() as async context manager with fetchrow -> None
            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchrow = AsyncMock(return_value=None)

            pipeline = DiscoveryPipeline()
            summary = await pipeline.run_for_countries(
                country_codes=["BG"],
                max_cities_per_country=1,
            )
            assert summary["countries_processed"] == 0
            assert summary["cities_processed"] == 0
            assert summary["results"]["BG"]["cities"] == 0
            assert summary["results"]["BG"]["agencies"] == 0

    async def test_run_for_countries_all_countries(self):
        """run_for_countries fetches all pending countries when none specified."""
        from agency_audit.discovery import DiscoveryPipeline

        with patch("agency_audit.discovery.get_pool") as mock_get_pool:
            mock_pool = MagicMock()
            mock_get_pool.return_value = mock_pool
            mock_pool.fetch = AsyncMock(
                return_value=[
                    {"country": "BG"},
                    {"country": "DE"},
                ]
            )

            # No pending cities in either country
            mock_conn = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value = mock_ctx
            mock_conn.fetchrow = AsyncMock(return_value=None)  # no city

            pipeline = DiscoveryPipeline()
            summary = await pipeline.run_for_countries(max_cities_per_country=1)
            assert "BG" in summary["results"]
            assert "DE" in summary["results"]


class TestDiscoverCLI:
    """Tests for the 'discover' CLI command and its help text."""

    def test_discover_command_registered(self):
        """'discover' command should be registered in the Typer app."""
        from agency_audit.cli import app

        commands = [c.name for c in app.registered_commands]
        assert "discover" in commands

    def test_discover_help_does_not_promise_browser_fallback(self):
        """The discover help text should NOT mention browser fallback.

        Browser-fallback discovery is not implemented -- the only path
        uses the Google Maps Places API, which requires an API key.
        """
        from typer.testing import CliRunner

        from agency_audit.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["discover", "--help"])
        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "browser" not in help_text, (
            "Discover CLI help text must not advertise browser fallback "
            "(not implemented). Help text: " + result.output
        )
        assert "api key" in help_text, (
            "Discover CLI help text must mention that an API key is required. "
            "Help text: " + result.output
        )
