"""Fixture-driven unit tests for listing_quality.py.

Covers: structured-data detection, prices, locations, images, map flags,
quality_score computation, and full assess_listing_quality integration.

All tests use synthetic HTML and httpx.MockTransport — no live network.
"""

from __future__ import annotations

import httpx
import pytest

from agency_audit.audit.listing_quality import (
    _check_map,
    _check_structured_data,
    assess_listing_quality,
)
from agency_audit.audit.models import AuditData

# ---------------------------------------------------------------------------
# Helper-function tests: _check_structured_data
# ---------------------------------------------------------------------------


class TestStructuredData:
    def test_jsonld_single_product(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Villa"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_jsonld_list(self):
        """JSON array with a real estate type."""
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '[{"@type": "Place", "name": "Office"}]'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_jsonld_graph(self):
        """@graph containing a real estate type."""
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@graph": [{"@type": "WebPage"}, {"@type": "RealEstateListing"}]}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_jsonld_multiple_types(self):
        """@type as an array with one matching type."""
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": ["Product", "Offer"], "name": "Villa"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_jsonld_invalid_json(self):
        """Malformed JSON silently skipped."""
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            "{broken json!!!"
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is False

    def test_jsonld_empty_string(self):
        """Empty script text skipped."""
        html = (
            '<html><head><script type="application/ld+json">   </script></head><body></body></html>'
        )
        assert _check_structured_data(html) is False

    def test_microdata_product(self):
        html = (
            "<html><body>"
            '<div itemtype="https://schema.org/Product">'
            '<span itemprop="name">Villa</span>'
            "</div>"
            "</body></html>"
        )
        assert _check_structured_data(html) is True

    def test_microdata_case_insensitive(self):
        html = (
            "<html><body>"
            '<div itemtype="https://schema.org/product">'
            "<span>Villa</span>"
            "</div>"
            "</body></html>"
        )
        assert _check_structured_data(html) is True

    def test_house_type(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "House", "name": "Villa"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_apartment_type(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Apartment", "name": "Condo"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_single_family_residence_type(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "SingleFamilyResidence", "name": "House"}'
            "</script>"
            "</head><body></body></html>"
        )
        assert _check_structured_data(html) is True

    def test_no_structured_data(self):
        html = "<html><body>Hello World</body></html>"
        assert _check_structured_data(html) is False


# ---------------------------------------------------------------------------
# Helper-function tests: _check_map
# ---------------------------------------------------------------------------


class TestMapDetection:
    def test_google_apis(self):
        html = '<iframe src="https://maps.googleapis.com/map/embed"></iframe>'
        assert _check_map(html) is True

    def test_google_embed(self):
        html = '<iframe src="https://www.google.com/maps/embed?pb=..."></iframe>'
        assert _check_map(html) is True

    def test_leaflet(self):
        html = '<div class="leaflet-container"></div>'
        assert _check_map(html) is True

    def test_mapbox(self):
        html = '<script src="https://api.mapbox.com/mapbox-gl-js/v2/mapbox-gl.js"></script>'
        assert _check_map(html) is True

    def test_openstreetmap(self):
        html = '<div id="map" data-source="openstreetmap"></div>'
        assert _check_map(html) is True

    def test_map_container_class(self):
        html = '<div class="map-container" id="prop-map"></div>'
        assert _check_map(html) is True

    def test_property_map_id(self):
        html = '<div id="property-map"></div>'
        assert _check_map(html) is True

    def test_listing_map_class(self):
        html = '<div class="listing-map"></div>'
        assert _check_map(html) is True

    def test_no_map(self):
        html = "<html><body><p>No map here</p></body></html>"
        assert _check_map(html) is False

    def test_sitemap_not_map(self):
        html = '<div class="sitemap"><a href="/page1">Page 1</a></div>'
        assert _check_map(html) is False

    def test_footer_map_excluded(self):
        html = '<div class="footer-map">Site Map</div>'
        assert _check_map(html) is False

    def test_imagemap_excluded(self):
        html = '<div class="imagemap"><img src="map.png"></div>'
        assert _check_map(html) is False

    def test_sitemap_map_excluded(self):
        html = '<div class="sitemap-map"><a href="/sitemap">Sitemap</a></div>'
        assert _check_map(html) is False


# ---------------------------------------------------------------------------
# Full assessment: assess_listing_quality (async, mocked httpx)
# ---------------------------------------------------------------------------


def _make_client(handler):
    """Create an httpx.AsyncClient with a MockTransport using handler."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


class TestAssessListingQuality:
    @pytest.mark.asyncio
    async def test_all_indicators_present(self):
        """Quality score 1.0 when all six indicators are present."""
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type": "Product", "name": "Villa"}'
            "</script>"
            "</head><body>"
            '<div class="property-item">'
            '<span class="price">200k</span>'
            '<span class="location">Sofia</span>'
            '<img src="/img/h1.jpg" alt="House">'
            '<p class="description">Nice villa</p>'
            "</div>"
            '<div id="property-map"></div>'
            "</body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality("https://example.com", client=client)
            assert result.has_structured_data is True
            assert result.has_prices is True
            assert result.has_locations is True
            assert result.has_images is True
            assert result.has_descriptions is True
            assert result.has_property_map is True
            assert result.quality_score == pytest.approx(1.0)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_all_indicators_absent(self):
        """Quality score 0.0 when nothing is present."""
        html = "<html><body><p>Plain page</p></body></html>"

        def handler(request):
            return httpx.Response(200, text=html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality("https://example.com", client=client)
            assert result.has_structured_data is False
            assert result.has_prices is False
            assert result.has_locations is False
            assert result.has_images is False
            assert result.has_descriptions is False
            assert result.has_property_map is False
            assert result.quality_score == pytest.approx(0.0)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_mixed_indicators(self):
        """Quality score 3/6 = 0.5 with three indicators."""
        html = (
            "<html><body>"
            '<div class="property-item">'
            '<span class="price">100k</span>'
            '<span class="location">Sofia</span>'
            '<img src="/img/h.jpg" alt="House">'
            "</div>"
            "</body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality("https://example.com", client=client)
            assert result.has_prices is True
            assert result.has_locations is True
            assert result.has_images is True
            assert result.has_structured_data is False
            assert result.has_descriptions is False
            assert result.has_property_map is False
            assert result.quality_score == pytest.approx(0.5)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_with_homepage_response(self):
        """Passing pre-fetched response skips HTTP fetch."""
        html = (
            "<html><body>"
            '<span class="price">150k</span>'
            '<span class="location">Plovdiv</span>'
            "</body></html>"
        )
        response = httpx.Response(
            200,
            text=html,
            request=httpx.Request("GET", "https://example.com"),
        )
        result = await assess_listing_quality("https://example.com", homepage_response=response)
        assert result.has_prices is True
        assert result.has_locations is True
        assert result.quality_score == pytest.approx(2.0 / 6.0)

    @pytest.mark.asyncio
    async def test_listing_page_fallback(self):
        """Listing page fills in missing indicators."""
        homepage_html = "<html><body><span class='price'>100k</span></body></html>"
        listing_html = (
            "<html><body>"
            '<div class="property">'
            '<span class="location">Sofia</span>'
            '<img src="/img/p.jpg" alt="Prop">'
            "</div>"
            "</body></html>"
        )

        def handler(request):
            url = str(request.url)
            if "/properties" in url:
                return httpx.Response(200, text=listing_html, request=request)
            return httpx.Response(200, text=homepage_html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality(
                "https://example.com",
                listing_url="https://example.com/properties",
                client=client,
            )
            assert result.has_prices is True
            assert result.has_locations is True
            assert result.has_images is True
            assert result.quality_score == pytest.approx(3.0 / 6.0)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_listing_page_no_override(self):
        """Listing page does not clobber already-found indicators."""
        homepage_html = (
            "<html><body>"
            '<span class="price">200k</span>'
            '<span class="location">Sofia</span>'
            "</body></html>"
        )
        listing_html = "<html><body></body></html>"

        def handler(request):
            url = str(request.url)
            if "/properties" in url:
                return httpx.Response(200, text=listing_html, request=request)
            return httpx.Response(200, text=homepage_html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality(
                "https://example.com",
                listing_url="https://example.com/properties",
                client=client,
            )
            assert result.has_prices is True
            assert result.has_locations is True
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Connection errors caught, default result returned."""

        def error_handler(request):
            raise httpx.ConnectError("Connection refused")

        client = _make_client(error_handler)
        try:
            result = await assess_listing_quality("https://down.test", client=client)
            assert result.has_structured_data is False
            assert result.quality_score == 0.0
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_to_dict_serialization(self):
        """Result serializes correctly via AuditData.to_dict."""
        html = (
            "<html><body>"
            '<span class="price">100k</span>'
            '<span class="location">Sofia</span>'
            "</body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, request=request)

        client = _make_client(handler)
        try:
            lq_result = await assess_listing_quality("https://example.com", client=client)
            audit = AuditData(url="https://example.com", listing_quality=lq_result)
            data = audit.to_dict()
            assert data["listings_have_prices"] is True
            assert data["listings_have_locations"] is True
            assert data["listing_quality_score"] == pytest.approx(2.0 / 6.0)
            assert "has_structured_data" in data
            assert "listings_have_images" in data
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_listing_page_404_graceful(self):
        """Listing page 404 handled gracefully."""
        homepage_html = "<html><body><span class='price'>100k</span></body></html>"

        def handler(request):
            url = str(request.url)
            if "/properties" in url:
                return httpx.Response(404, text="Not Found", request=request)
            return httpx.Response(200, text=homepage_html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality(
                "https://example.com",
                listing_url="https://example.com/properties",
                client=client,
            )
            assert result.has_prices is True
            assert result.quality_score == pytest.approx(1.0 / 6.0)
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_has_descriptions(self):
        """Descriptions detected via .description selector."""
        html = (
            "<html><body>"
            '<div class="property-item">'
            '<p class="description">Great place</p>'
            "</div>"
            "</body></html>"
        )

        def handler(request):
            return httpx.Response(200, text=html, request=request)

        client = _make_client(handler)
        try:
            result = await assess_listing_quality("https://example.com", client=client)
            assert result.has_descriptions is True
        finally:
            await client.aclose()
