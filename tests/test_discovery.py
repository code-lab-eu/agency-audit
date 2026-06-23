"""Tests for PlacesAPIClient.search_text — request construction,
response mapping, pagination, rate-limit integration, and error paths.

All tests use httpx.MockTransport — no live network or database.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from agency_audit.discovery import PlacesAPIClient

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def make_mock_client(handler) -> PlacesAPIClient:
    """Create a PlacesAPIClient with an httpx.AsyncClient backed by
    MockTransport, so calls to search_text hit the mock without any
    live network."""
    client = PlacesAPIClient(api_key="mock-key")
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(transport=transport)
    return client


def build_place_response(places: list[dict], next_page_token: str | None = None) -> dict:
    """Build a Places API (New) response dict."""
    resp: dict = {"places": places}
    if next_page_token:
        resp["nextPageToken"] = next_page_token
    return resp


# ──────────────────────────────────────────────────────────────────────
# Request construction
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientSearchTextRequest:
    """Verify that search_text builds the correct POST body."""

    async def test_minimal_request(self):
        """A query with no location_bias produces minimal body."""
        captured_body = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_body
            captured_body = request.read().decode("utf-8")
            import json
            captured_body = json.loads(captured_body)
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        await client.search_text("estate agent Sofia", max_results=20)
        await client.close()

        assert captured_body is not None
        assert captured_body["textQuery"] == "estate agent Sofia"
        assert captured_body["pageSize"] == 20
        assert "locationBias" not in captured_body

    async def test_request_with_location_bias(self):
        """A query with lat/lng includes a locationBias circle."""
        captured_body = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_body
            import json
            captured_body = json.loads(request.read().decode("utf-8"))
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        await client.search_text(
            "Immobilienmakler Berlin",
            location_bias=(52.5200, 13.4050),
            radius=5000,
            max_results=20,
        )
        await client.close()

        assert captured_body is not None
        bias = captured_body["locationBias"]
        assert bias["circle"]["center"]["latitude"] == 52.5200
        assert bias["circle"]["center"]["longitude"] == 13.4050
        assert bias["circle"]["radius"] == 5000

    async def test_page_size_clamped_to_max_results(self):
        """pageSize must not exceed the remaining results needed."""
        captured_sizes = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            body = json.loads(request.read().decode("utf-8"))
            captured_sizes.append(body["pageSize"])
            # Return no places so search_text exits immediately
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        await client.search_text("test", max_results=15)
        await client.close()

        assert captured_sizes == [15]


# ──────────────────────────────────────────────────────────────────────
# Response → PlaceResult mapping
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientSearchTextResponse:
    """Verify that Places API JSON is correctly mapped to PlaceResult."""

    async def test_maps_complete_place(self):
        """All fields present in response → PlaceResult populated."""
        place_json = {
            "id": "ChIJ-test-123",
            "displayName": {"text": "Awesome Real Estate"},
            "formattedAddress": "ul. Tsar Osvoboditel 1, Sofia",
            "internationalPhoneNumber": "+359 2 123 4567",
            "websiteUri": "https://awesome-re.bg",
            "location": {"latitude": 42.6977, "longitude": 23.3219},
            "rating": 4.8,
            "userRatingCount": 234,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response([place_json]), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("test query", max_results=20)
        await client.close()

        assert len(results) == 1
        r = results[0]
        assert r.place_id == "ChIJ-test-123"
        assert r.name == "Awesome Real Estate"
        assert r.formatted_address == "ul. Tsar Osvoboditel 1, Sofia"
        assert r.phone == "+359 2 123 4567"
        assert r.website == "https://awesome-re.bg"
        assert r.latitude == 42.6977
        assert r.longitude == 23.3219
        assert r.rating == 4.8
        assert r.user_ratings_total == 234

    async def test_maps_minimal_place(self):
        """Missing optional fields → None defaults on PlaceResult."""
        place_json = {
            "id": "minimal-1",
            "displayName": {"text": "Minimal Agency"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response([place_json]), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=20)
        await client.close()

        r = results[0]
        assert r.place_id == "minimal-1"
        assert r.name == "Minimal Agency"
        assert r.formatted_address is None
        assert r.phone is None
        assert r.website is None
        assert r.latitude is None
        assert r.longitude is None
        assert r.rating is None
        assert r.user_ratings_total is None

    async def test_maps_place_without_location(self):
        """location dict present but lat/lng keys missing."""
        place_json = {
            "id": "no-loc-1",
            "displayName": {"text": "No Location Co"},
            "location": {},  # no latitude/longitude
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response([place_json]), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=20)
        await client.close()

        assert results[0].latitude is None
        assert results[0].longitude is None

    async def test_maps_place_rating_zero(self):
        """rating=0 is a valid value, not coerced to None."""
        place_json = {
            "id": "zero-rating",
            "displayName": {"text": "Zero Stars"},
            "rating": 0,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response([place_json]), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=20)
        await client.close()

        assert results[0].rating == 0

    async def test_maps_multiple_places(self):
        """Multiple places in a single response page."""
        places_json = [
            {"id": "p1", "displayName": {"text": "Agency One"}, "rating": 4.0},
            {"id": "p2", "displayName": {"text": "Agency Two"}, "rating": 4.5},
            {"id": "p3", "displayName": {"text": "Agency Three"}, "rating": 3.8},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response(places_json), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=20)
        await client.close()

        assert len(results) == 3
        assert results[0].name == "Agency One"
        assert results[1].name == "Agency Two"
        assert results[2].name == "Agency Three"

    async def test_empty_results(self):
        """Empty places array → empty list returned."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=build_place_response([]), request=request
            )

        client = make_mock_client(handler)
        results = await client.search_text("nonexistent", max_results=20)
        await client.close()

        assert results == []


# ──────────────────────────────────────────────────────────────────────
# Pagination
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientSearchTextPagination:
    """Verify multi-page traversal via nextPageToken."""

    async def test_paginates_across_pages(self):
        """Two pages of results are concatenated."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            import json
            body = json.loads(request.read().decode("utf-8"))

            if call_count == 1:
                assert "pageToken" not in body
                return httpx.Response(
                    200,
                    json=build_place_response(
                        [
                            {"id": f"p1-{i}", "displayName": {"text": f"Page1-{i}"}}
                            for i in range(10)
                        ],
                        next_page_token="token-abc",
                    ),
                    request=request,
                )
            else:
                assert body.get("pageToken") == "token-abc"
                return httpx.Response(
                    200,
                    json=build_place_response(
                        [
                            {"id": f"p2-{i}", "displayName": {"text": f"Page2-{i}"}}
                            for i in range(10)
                        ]
                    ),
                    request=request,
                )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=30)
        await client.close()

        assert len(results) == 20
        assert call_count == 2
        assert results[0].name == "Page1-0"
        assert results[-1].name == "Page2-9"

    async def test_respects_max_results(self):
        """Results should not exceed max_results, even with more pages."""
        def handler(request: httpx.Request) -> httpx.Response:
            import json
            body = json.loads(request.read().decode("utf-8"))
            # Return exactly what was asked for (pageSize), plus a next token
            # to test that we stop when max_results is hit
            page_size = body["pageSize"]
            places = [
                {"id": f"p-{i}", "displayName": {"text": f"Place {i}"}}
                for i in range(page_size)
            ]
            return httpx.Response(
                200,
                json=build_place_response(places, next_page_token="more-token"),
                request=request,
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=25)
        await client.close()

        assert len(results) == 25

    async def test_stops_when_no_next_page_token(self):
        """If nextPageToken is absent, stop paginating."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json=build_place_response(
                    [
                        {"id": "single", "displayName": {"text": "Only Place"}}
                    ]
                    # No nextPageToken
                ),
                request=request,
            )

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=60)
        await client.close()

        assert len(results) == 1
        assert call_count == 1


# ──────────────────────────────────────────────────────────────────────
# Error paths
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientSearchTextErrors:
    """HTTP errors and timeouts during search_text."""

    async def test_http_403_raises(self):
        """HTTP status errors propagate as httpx.HTTPStatusError."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"}, request=request)

        client = make_mock_client(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await client.search_text("test", max_results=20)

        await client.close()

    async def test_http_429_raises(self):
        """Rate-limit response from API propagates."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"error": "quota exceeded"},
                headers={"Retry-After": "1"},
                request=request,
            )

        client = make_mock_client(handler)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.search_text("test", max_results=20)
        assert exc_info.value.response.status_code == 429

        await client.close()

    async def test_http_500_raises(self):
        """Server errors propagate."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"}, request=request)

        client = make_mock_client(handler)

        with pytest.raises(httpx.HTTPStatusError):
            await client.search_text("test", max_results=20)

        await client.close()

    async def test_timeout_breaks_gracefully(self):
        """TimeoutException stops pagination without raising — returns partial results."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("request timed out")

        client = make_mock_client(handler)

        # TimeoutException is caught and breaks the loop, returning empty results
        results = await client.search_text("test", max_results=20)
        await client.close()

        assert results == []

    async def test_timeout_after_partial_results(self):
        """Timeout on page 2: page 1 results are preserved."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json=build_place_response(
                        [
                            {"id": "p1", "displayName": {"text": "First Page"}}
                        ],
                        next_page_token="token-next",
                    ),
                    request=request,
                )
            else:
                raise httpx.TimeoutException("timed out on page 2")

        client = make_mock_client(handler)
        results = await client.search_text("test", max_results=60)
        await client.close()

        assert len(results) == 1
        assert results[0].name == "First Page"


# ──────────────────────────────────────────────────────────────────────
# Rate-limit integration with search_text
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientSearchTextRateLimit:
    """Verify rate-limiting is called as part of search_text flow."""

    async def test_rate_limit_called_before_search(self):
        """_rate_limit is invoked before the HTTP call."""
        call_order = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_order.append("http")
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)

        # Wrap _rate_limit to track order
        original_rate_limit = client._rate_limit

        async def tracked_rate_limit():
            call_order.append("rate_limit")
            await original_rate_limit()

        client._rate_limit = tracked_rate_limit

        await client.search_text("test", max_results=20)
        await client.close()

        # _rate_limit must fire before the HTTP call
        assert call_order[0] == "rate_limit"
        assert "http" in call_order

    async def test_rate_limit_tracks_request_count(self):
        """_request_count increments after search_text."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        assert client._request_count == 0

        await client.search_text("q1", max_results=20)
        assert client._request_count == 1

        await client.search_text("q2", max_results=20)
        assert client._request_count == 2

        await client.close()


# ──────────────────────────────────────────────────────────────────────
# API key resolution (edge cases already covered in test_geonames_discovery.py)
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientKeyResolution:
    """Edge cases around API key loading not covered in test_geonames_discovery.py."""

    def test_explicit_key_overrides_settings(self):
        """An explicit api_key arg bypasses _load_api_key entirely."""
        with patch("agency_audit.config.settings") as mock_settings:
            mock_settings.google_maps_api_key = "from-env"
            client = PlacesAPIClient(api_key="explicit-key")
            # Should use the explicit key, not the env one
            assert client.api_key == "explicit-key"

    def test_env_var_goog_maps_api_key_fallback(self):
        """GOOGLE_MAPS_API_KEY (no prefix) is read by pydantic-settings
        since google_maps_api_key is the field name, and pydantic-settings
        checks both AGENCY_AUDIT_GOOGLE_MAPS_API_KEY and GOOGLE_MAPS_API_KEY."""
        import os

        with patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "bare-env-key"}), \
             patch("agency_audit.config.settings") as mock_settings:
                mock_settings.google_maps_api_key = "bare-env-key"
                client = PlacesAPIClient()
                assert client.api_key == "bare-env-key"
                assert client.available is True

    async def test_run_discovery_raises_when_no_key(self):
        """run_discovery raises RuntimeError when api_key is empty."""
        from unittest.mock import AsyncMock

        from agency_audit.discovery import run_discovery

        with patch("agency_audit.discovery.DiscoveryPipeline") as mock_pipeline_cls:
            mock_pipeline = mock_pipeline_cls.return_value
            mock_pipeline.places.available = False
            mock_pipeline.close = AsyncMock()

            with pytest.raises(RuntimeError, match="No Google Maps API key"):
                await run_discovery()

    async def test_run_discovery_returns_summary_with_key(self):
        """run_discovery succeeds when api_key is available."""
        from unittest.mock import AsyncMock

        from agency_audit.discovery import run_discovery

        with patch("agency_audit.discovery.DiscoveryPipeline") as mock_pipeline_cls:
            mock_pipeline = mock_pipeline_cls.return_value
            mock_pipeline.places.available = True
            mock_pipeline.run_for_countries = AsyncMock(
                return_value={"countries_processed": 1, "cities_processed": 2}
            )
            mock_pipeline.close = AsyncMock()

            summary = await run_discovery(countries=["BG"], max_cities=2)
            assert summary["countries_processed"] == 1
            assert summary["cities_processed"] == 2
            mock_pipeline.close.assert_called()


# ──────────────────────────────────────────────────────────────────────
# ensure_client headers and configuration
# ──────────────────────────────────────────────────────────────────────


class TestPlacesAPIClientConfiguration:
    """Verify the httpx.AsyncClient is configured correctly."""

    async def test_headers_include_api_key(self):
        """API key is set in X-Goog-Api-Key header."""
        captured_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_headers
            captured_headers = dict(request.headers)
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = PlacesAPIClient(api_key="mock-key")
        transport = httpx.MockTransport(handler)

        # Inject mock transport without bypassing ensure_client header setup
        orig_init = httpx.AsyncClient.__init__

        def patched_init(self_, **kwargs):
            kwargs["transport"] = transport
            return orig_init(self_, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", patched_init):
            await client.search_text("test", max_results=20)

        await client.close()

        assert captured_headers.get("x-goog-api-key") == "mock-key"

    async def test_field_mask_is_set(self):
        """X-Goog-FieldMask header includes the expected fields."""
        captured_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_headers
            captured_headers = dict(request.headers)
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = PlacesAPIClient(api_key="mock-key")
        transport = httpx.MockTransport(handler)

        # Inject mock transport without bypassing ensure_client header setup
        orig_init = httpx.AsyncClient.__init__

        def patched_init(self_, **kwargs):
            kwargs["transport"] = transport
            return orig_init(self_, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", patched_init):
            await client.search_text("test", max_results=20)

        await client.close()

        field_mask = captured_headers.get("x-goog-fieldmask", "")
        assert "places.id" in field_mask
        assert "places.displayName" in field_mask
        assert "places.location" in field_mask

    async def test_content_type_is_json(self):
        """Content-Type header is application/json."""
        captured_headers = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_headers
            captured_headers = dict(request.headers)
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        await client._ensure_client()
        await client.search_text("test", max_results=20)
        await client.close()

        assert captured_headers is not None
        assert captured_headers["content-type"] == "application/json"

    async def test_post_url_is_base_url(self):
        """POST goes to the correct endpoint."""
        captured_url = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured_url
            captured_url = str(request.url)
            return httpx.Response(200, json=build_place_response([]), request=request)

        client = make_mock_client(handler)
        await client.search_text("test", max_results=20)
        await client.close()

        assert captured_url == PlacesAPIClient.BASE_URL
