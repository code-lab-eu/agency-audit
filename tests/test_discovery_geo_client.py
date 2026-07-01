"""Tests for GeocodingClient and DiscoveryPipeline.resolve_city_viewport.

All tests mock httpx (via MockTransport) and the database pool — no live
network or database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agency_audit.config import settings
from agency_audit.discovery import DiscoveryPipeline, GeocodingClient
from agency_audit.discovery_geo import bbox_from_center

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _geocode_json_response(lat: float, lon: float) -> dict:
    """Build a successful Geocoding API JSON response with a viewport
    centred on (*lat*, *lon*)."""
    return {
        "results": [
            {
                "geometry": {
                    "viewport": {
                        "northeast": {"lat": lat + 0.1, "lng": lon + 0.1},
                        "southwest": {"lat": lat - 0.1, "lng": lon - 0.1},
                    }
                }
            }
        ],
        "status": "OK",
    }


def make_geocoding_client(handler) -> GeocodingClient:
    """Create a GeocodingClient backed by an httpx.MockTransport."""
    client = GeocodingClient(api_key="mock-key")
    transport = httpx.MockTransport(handler)
    client._client = httpx.AsyncClient(transport=transport)
    return client


def make_city_dict(
    *,
    city_id: int = 1,
    label: str = "Sofia",
    country: str = "BG",
    latitude: float = 42.6977,
    longitude: float = 23.3219,
    viewport_low_lat: float | None = None,
    viewport_low_lng: float | None = None,
    viewport_high_lat: float | None = None,
    viewport_high_lng: float | None = None,
) -> dict:
    """Build a city dict that mimics an asyncpg.Record for the viewport columns."""
    return dict(
        id=city_id,
        label=label,
        country=country,
        latitude=latitude,
        longitude=longitude,
        viewport_low_lat=viewport_low_lat,
        viewport_low_lng=viewport_low_lng,
        viewport_high_lat=viewport_high_lat,
        viewport_high_lng=viewport_high_lng,
    )


# ──────────────────────────────────────────────────────────────────────
# (a) Cache hit — stored viewport, no API call
# ──────────────────────────────────────────────────────────────────────


class TestResolveCityViewportCacheHit:
    """City already has viewport columns — return them without geocoding."""

    async def test_cache_hit_no_api_call(self):
        """Stored viewport columns → returned directly, no HTTP request."""
        geocode_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            geocode_calls.append(request)
            return httpx.Response(200, json=_geocode_json_response(42.0, 23.0))

        geo_client = make_geocoding_client(handler)

        mock_pool = AsyncMock()
        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(
                viewport_low_lat=42.5,
                viewport_low_lng=23.2,
                viewport_high_lat=42.8,
                viewport_high_lng=23.5,
            )

            rect = await pipeline.resolve_city_viewport(city)

            # No API call was made
            assert len(geocode_calls) == 0
            # Returned the cached values
            assert rect.low_lat == 42.5
            assert rect.low_lng == 23.2
            assert rect.high_lat == 42.8
            assert rect.high_lng == 23.5

            await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# (b) Cache miss — geocode, persist, return
# ──────────────────────────────────────────────────────────────────────


class TestResolveCityViewportCacheMiss:
    """City missing viewport columns — geocode, persist, return Rectangle."""

    async def test_geocodes_and_persists(self):
        """City without viewport: one geocode call, persist SQL, return Rectangle."""
        geocode_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            geocode_calls.append(request)
            return httpx.Response(200, json=_geocode_json_response(42.7, 23.3))

        geo_client = make_geocoding_client(handler)

        mock_conn = AsyncMock()
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(label="Sofia", country="BG")

            rect = await pipeline.resolve_city_viewport(city)

            # Exactly one geocode call
            assert len(geocode_calls) == 1

            # Verify the address passed to the API
            url = str(geocode_calls[0].url)
            assert "Sofia%2C+BG" in url or "Sofia,+BG" in url

            # Returns the expected Rectangle
            assert rect.low_lat == pytest.approx(42.6)
            assert rect.low_lng == pytest.approx(23.2)
            assert rect.high_lat == pytest.approx(42.8)
            assert rect.high_lng == pytest.approx(23.4)

            # Pool was used to persist
            assert mock_pool.acquire.called

            # Verify the UPDATE SQL was called with correct values
            execute_call = mock_conn.execute
            assert execute_call.called
            args, _kwargs = execute_call.call_args
            assert "UPDATE cities" in args[0]
            assert "viewport_low_lat" in args[0]
            assert "viewport_low_lng" in args[0]
            assert "viewport_high_lat" in args[0]
            assert "viewport_high_lng" in args[0]
            # Check that the values passed match the geocoded rect
            # (params are positional: $1-$4 for the four viewport values, $5 for id)
            assert float(args[1]) == pytest.approx(42.6)  # low_lat
            assert float(args[2]) == pytest.approx(23.2)  # low_lng
            assert float(args[3]) == pytest.approx(42.8)  # high_lat
            assert float(args[4]) == pytest.approx(23.4)  # high_lng
            assert args[5] == 1  # city id

            await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# (c) Geocoding failure — fall back to bbox_from_center
# ──────────────────────────────────────────────────────────────────────


class TestResolveCityViewportFallback:
    """Geocoding failures fall back to bbox_from_center."""

    async def test_http_error_falls_back(self, caplog):
        """HTTP 500 from geocoding → fall back to bbox_from_center."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        geo_client = make_geocoding_client(handler)

        mock_pool = AsyncMock()
        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(latitude=42.6977, longitude=23.3219)

            rect = await pipeline.resolve_city_viewport(city)

            # Falls back to bbox_from_center
            expected = bbox_from_center(42.6977, 23.3219, settings.places_city_half_extent_meters)
            assert rect.low_lat == pytest.approx(expected.low_lat)
            assert rect.low_lng == pytest.approx(expected.low_lng)
            assert rect.high_lat == pytest.approx(expected.high_lat)
            assert rect.high_lng == pytest.approx(expected.high_lng)

            await pipeline.close()

    async def test_zero_results_falls_back(self, caplog):
        """Geocoding returns ZERO_RESULTS → fall back to bbox_from_center."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [], "status": "ZERO_RESULTS"},
            )

        geo_client = make_geocoding_client(handler)

        mock_pool = AsyncMock()
        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(latitude=42.6977, longitude=23.3219)

            rect = await pipeline.resolve_city_viewport(city)

            expected = bbox_from_center(42.6977, 23.3219, settings.places_city_half_extent_meters)
            assert rect.low_lat == pytest.approx(expected.low_lat)

            await pipeline.close()

    async def test_no_viewport_in_result_falls_back(self, caplog):
        """Geocoding result has no geometry.viewport → fall back."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"geometry": {}}],  # no viewport key
                    "status": "OK",
                },
            )

        geo_client = make_geocoding_client(handler)

        mock_pool = AsyncMock()
        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(latitude=42.6977, longitude=23.3219)

            rect = await pipeline.resolve_city_viewport(city)

            expected = bbox_from_center(42.6977, 23.3219, settings.places_city_half_extent_meters)
            assert rect.low_lat == pytest.approx(expected.low_lat)

            await pipeline.close()

    async def test_timeout_falls_back(self, caplog):
        """TimeoutException from geocoding → fall back to bbox_from_center."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        geo_client = make_geocoding_client(handler)

        mock_pool = AsyncMock()
        with patch.object(DiscoveryPipeline, "_get_pool", AsyncMock(return_value=mock_pool)):
            pipeline = DiscoveryPipeline(
                places_client=AsyncMock(),
                geocoding_client=geo_client,
            )

            city = make_city_dict(latitude=42.6977, longitude=23.3219)

            rect = await pipeline.resolve_city_viewport(city)

            expected = bbox_from_center(42.6977, 23.3219, settings.places_city_half_extent_meters)
            assert rect.low_lat == pytest.approx(expected.low_lat)

            await pipeline.close()


# ──────────────────────────────────────────────────────────────────────
# GeocodingClient
# ──────────────────────────────────────────────────────────────────────


class TestGeocodingClient:
    """Unit tests for GeocodingClient.geocode()."""

    async def test_geocode_success(self):
        """Successful geocode returns parsed JSON with status OK."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_geocode_json_response(42.7, 23.3))

        client = make_geocoding_client(handler)
        result = await client.geocode("Sofia, BG")
        await client.close()

        assert result is not None
        assert result["status"] == "OK"
        assert "results" in result

    async def test_geocode_http_error_returns_none(self):
        """HTTP error → None."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        client = make_geocoding_client(handler)
        result = await client.geocode("test")
        await client.close()

        assert result is None

    async def test_geocode_non_ok_status_returns_none(self):
        """Non-OK status (e.g. ZERO_RESULTS) → None."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [], "status": "ZERO_RESULTS"},
            )

        client = make_geocoding_client(handler)
        result = await client.geocode("nonexistent")
        await client.close()

        assert result is None

    async def test_geocode_empty_results_returns_none(self):
        """Empty results array → None."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [], "status": "OK"},
            )

        client = make_geocoding_client(handler)
        result = await client.geocode("empty")
        await client.close()

        assert result is None

    async def test_geocode_timeout_returns_none(self):
        """TimeoutException → None."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        client = make_geocoding_client(handler)
        result = await client.geocode("test")
        await client.close()

        assert result is None

    async def test_geocode_request_count(self):
        """_request_count increments after each geocode call."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_geocode_json_response(42.0, 23.0))

        client = make_geocoding_client(handler)
        assert client._request_count == 0

        await client.geocode("Sofia, BG")
        assert client._request_count == 1

        await client.geocode("Plovdiv, BG")
        assert client._request_count == 2

        await client.close()

    async def test_close_cleans_up(self):
        """close() cleans up the httpx client."""
        client = GeocodingClient(api_key="test-key")
        await client._ensure_client()  # force client creation
        assert client._client is not None

        await client.close()
        assert client._client is None
